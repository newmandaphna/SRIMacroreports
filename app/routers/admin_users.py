"""Administrator user management.

Users are created here with a temporary password and a forced change on first login.
They are deactivated, never deleted, so that audit entries always resolve to a real
identity.
"""

from __future__ import annotations

import secrets
import string
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from app.models.enums import AuditAction, AuditResult, Module, Role
from app.models.session import UserSession
from app.models.user import ModuleGrant, User
from app.security import audit
from app.security.deps import AdminUser, DbSession
from app.security.passwords import PasswordPolicyError, hash_password, validate_password
from app.security.sessions import revoke_all_for_user
from app.templating import render

router = APIRouter(prefix="/admin/users", tags=["admin"])

TEMP_PASSWORD_LENGTH = 16
# Ambiguous glyphs removed: a temporary password gets read aloud or typed off a note.
_TEMP_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "Il1O0")


def generate_temporary_password() -> str:
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(TEMP_PASSWORD_LENGTH))


@router.get("", response_class=HTMLResponse)
def list_users(request: Request, db: DbSession, auth: AdminUser) -> Response:
    users = db.execute(select(User).order_by(User.is_active.desc(), User.email)).scalars().all()

    live_counts = dict(
        db.execute(
            select(UserSession.user_id, func.count(UserSession.id))
            .where(UserSession.revoked_at.is_(None))
            .group_by(UserSession.user_id)
        ).all()
    )

    return render(
        request,
        "admin/users.html",
        {
            "page_title": "Users",
            "auth": auth,
            "users": users,
            "live_counts": live_counts,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_user_form(request: Request, auth: AdminUser) -> Response:
    return render(
        request,
        "admin/user_form.html",
        {"page_title": "Add user", "auth": auth, "user": None},
    )


@router.post("/new")
def create_user(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    email: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    role: Annotated[str, Form()],
    modules: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI form list default
) -> Response:
    email_normalized = email.strip().lower()
    display_name = display_name.strip()

    error = _validate_new_user(db, email_normalized, display_name, role)
    if error:
        return render(
            request,
            "admin/user_form.html",
            {
                "page_title": "Add user",
                "auth": auth,
                "user": None,
                "error": error,
                "form": {
                    "email": email_normalized,
                    "display_name": display_name,
                    "role": role,
                    "modules": modules,
                },
            },
            status_code=400,
        )

    temp_password = generate_temporary_password()
    user = User(
        email=email_normalized,
        display_name=display_name,
        role=Role(role),
        password_hash=hash_password(temp_password),
        must_change_password=True,
        is_active=True,
        created_by_id=auth.user.id,
    )
    db.add(user)
    db.flush()

    granted = _apply_grants(db, user, modules, actor_id=auth.user.id)

    audit.record(
        db,
        action=AuditAction.USER_CREATED,
        actor=auth.user,
        target_type="user",
        target_id=user.id,
        request=request,
        detail={"email": email_normalized, "role": role, "grants": sorted(granted)},
    )

    # Shown once, on the next page, and never stored in readable form.
    return render(
        request,
        "admin/user_created.html",
        {
            "page_title": "User created",
            "auth": auth,
            "new_user": user,
            "temporary_password": temp_password,
        },
        status_code=201,
    )


def _validate_new_user(db: DbSession, email: str, display_name: str, role: str) -> str | None:
    if "@" not in email or len(email) < 5:
        return "Enter a valid email address."
    if not display_name:
        return "Enter a display name."
    if role not in {r.value for r in Role}:
        return "Choose a valid role."
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        return (
            "An account already exists for that email address. "
            "Reactivate it instead of creating a second one."
        )
    return None


def _apply_grants(db: DbSession, user: User, modules: list[str], *, actor_id: int) -> set[str]:
    valid = {m.value for m in Module}
    wanted = {m for m in modules if m in valid}
    for module in sorted(wanted):
        db.add(ModuleGrant(user_id=user.id, module=Module(module), granted_by_id=actor_id))
    return wanted


@router.get("/{user_id}", response_class=HTMLResponse)
def edit_user_form(request: Request, db: DbSession, auth: AdminUser, user_id: int) -> Response:
    user = db.get(User, user_id)
    if user is None:
        return render(
            request,
            "errors/not_found.html",
            {"page_title": "Not found", "auth": auth},
            status_code=404,
        )
    return render(
        request,
        "admin/user_form.html",
        {"page_title": f"Edit {user.display_name}", "auth": auth, "user": user},
    )


@router.post("/{user_id}")
def update_user(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    user_id: int,
    display_name: Annotated[str, Form()],
    role: Annotated[str, Form()],
    modules: Annotated[list[str], Form()] = [],  # noqa: B006
) -> Response:
    user = db.get(User, user_id)
    if user is None:
        return render(
            request,
            "errors/not_found.html",
            {"page_title": "Not found", "auth": auth},
            status_code=404,
        )

    if role not in {r.value for r in Role}:
        return _edit_error(request, auth, user, "Choose a valid role.")

    # An admin must not be able to lock the practice out of its own administration.
    if _would_remove_last_admin(db, user, Role(role)):
        return _edit_error(
            request,
            auth,
            user,
            "This is the only active administrator. Promote someone else first.",
        )

    old_role = user.role_enum
    old_grants = {g.value for g in user.granted_modules}

    user.display_name = display_name.strip() or user.display_name
    user.role = Role(role)

    valid = {m.value for m in Module}
    new_grants = {m for m in modules if m in valid}

    for grant in list(user.grants):
        if grant.module not in new_grants:
            db.delete(grant)
    for module in sorted(new_grants - old_grants):
        db.add(ModuleGrant(user_id=user.id, module=Module(module), granted_by_id=auth.user.id))

    if old_role != user.role_enum:
        audit.record(
            db,
            action=AuditAction.ROLE_CHANGED,
            actor=auth.user,
            target_type="user",
            target_id=user.id,
            request=request,
            detail={"from": old_role.value, "to": role},
        )
    for added in sorted(new_grants - old_grants):
        audit.record(
            db,
            action=AuditAction.GRANT_ADDED,
            actor=auth.user,
            target_type="user",
            target_id=user.id,
            request=request,
            detail={"module": added},
        )
    for removed in sorted(old_grants - new_grants):
        audit.record(
            db,
            action=AuditAction.GRANT_REMOVED,
            actor=auth.user,
            target_type="user",
            target_id=user.id,
            request=request,
            detail={"module": removed},
        )

    audit.record(
        db,
        action=AuditAction.USER_UPDATED,
        actor=auth.user,
        target_type="user",
        target_id=user.id,
        request=request,
    )

    # A change to what someone may see takes effect now, not at their next login.
    if old_role != user.role_enum or old_grants != new_grants:
        revoke_all_for_user(db, user.id)

    return RedirectResponse("/admin/users", status_code=303)


def _edit_error(request: Request, auth: AdminUser, user: User, message: str) -> Response:
    return render(
        request,
        "admin/user_form.html",
        {
            "page_title": f"Edit {user.display_name}",
            "auth": auth,
            "user": user,
            "error": message,
        },
        status_code=400,
    )


def _would_remove_last_admin(db: DbSession, user: User, new_role: Role) -> bool:
    if user.role_enum is not Role.ADMIN or new_role is Role.ADMIN:
        return False
    return _active_admin_count(db) <= 1


def _active_admin_count(db: DbSession) -> int:
    return (
        db.execute(
            select(func.count(User.id)).where(User.role == Role.ADMIN, User.is_active.is_(True))
        ).scalar_one()
        or 0
    )


@router.post("/{user_id}/deactivate")
def deactivate_user(request: Request, db: DbSession, auth: AdminUser, user_id: int) -> Response:
    user = db.get(User, user_id)
    if user is None:
        return RedirectResponse("/admin/users", status_code=303)

    if user.id == auth.user.id:
        return _list_with_error(request, db, auth, "You cannot deactivate your own account.")
    if user.role_enum is Role.ADMIN and _active_admin_count(db) <= 1:
        return _list_with_error(request, db, auth, "This is the only active administrator.")

    user.is_active = False
    revoked = revoke_all_for_user(db, user.id)
    audit.record(
        db,
        action=AuditAction.USER_DEACTIVATED,
        actor=auth.user,
        target_type="user",
        target_id=user.id,
        request=request,
        detail={"sessions_revoked": revoked},
    )
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/{user_id}/reactivate")
def reactivate_user(request: Request, db: DbSession, auth: AdminUser, user_id: int) -> Response:
    user = db.get(User, user_id)
    if user is not None and not user.is_active:
        user.is_active = True
        audit.record(
            db,
            action=AuditAction.USER_REACTIVATED,
            actor=auth.user,
            target_type="user",
            target_id=user.id,
            request=request,
        )
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/{user_id}/reset-password")
def reset_password(request: Request, db: DbSession, auth: AdminUser, user_id: int) -> Response:
    user = db.get(User, user_id)
    if user is None:
        return RedirectResponse("/admin/users", status_code=303)

    temp_password = generate_temporary_password()
    user.password_hash = hash_password(temp_password)
    user.must_change_password = True
    user.failed_login_count = 0
    user.locked_until = None

    revoked = revoke_all_for_user(db, user.id)
    audit.record(
        db,
        action=AuditAction.USER_PASSWORD_RESET,
        actor=auth.user,
        target_type="user",
        target_id=user.id,
        request=request,
        detail={"sessions_revoked": revoked},
    )

    return render(
        request,
        "admin/user_created.html",
        {
            "page_title": "Password reset",
            "auth": auth,
            "new_user": user,
            "temporary_password": temp_password,
            "was_reset": True,
        },
    )


def _list_with_error(request: Request, db: DbSession, auth: AdminUser, message: str) -> Response:
    users = db.execute(select(User).order_by(User.is_active.desc(), User.email)).scalars().all()
    audit.record(
        db,
        action=AuditAction.ACCESS_DENIED,
        result=AuditResult.FAILURE,
        actor=auth.user,
        request=request,
        detail={"reason": message},
    )
    return render(
        request,
        "admin/users.html",
        {
            "page_title": "Users",
            "auth": auth,
            "users": users,
            "live_counts": {},
            "error": message,
        },
        status_code=400,
    )


# Kept importable for the seeding code and the tests.
__all__ = ["generate_temporary_password", "router", "validate_password", "PasswordPolicyError"]
