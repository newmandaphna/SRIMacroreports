"""Route dependencies: authentication, authorization, and the idle clock.

Everything here is server side. Hiding a nav link is a usability choice, never a
security control, so each protected route depends on one of these regardless of what
the UI shows (SECURITY.md section 3.3).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.enums import AuditAction, AuditResult, Module, Role
from app.models.session import UserSession
from app.models.user import User
from app.security import audit
from app.security.sessions import SESSION_COOKIE, load_session, revoke, touch

LOGIN_PATH = "/login"
CHANGE_PASSWORD_PATH = "/change-password"  # noqa: S105 - a URL path, not a secret


class RedirectRequired(Exception):
    """Raised by a dependency to send the browser somewhere else.

    Not an HTTPException because the desired response is a 303 with a Location, and
    because the exception handler adds the `next` parameter so a user lands back where
    they were trying to go.
    """

    def __init__(self, location: str, *, status_code: int = 303) -> None:
        self.location = location
        self.status_code = status_code
        super().__init__(location)


class AccessDenied(Exception):
    """Raised when an authenticated user lacks the role or grant for a route."""

    def __init__(self, message: str = "You do not have access to this area.") -> None:
        self.message = message
        super().__init__(message)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    """Request scoped database session.

    RedirectRequired and AccessDenied are control flow, not failures, and the work
    done before they are raised must survive. That work is exactly the work an
    auditor cares about: the ACCESS_DENIED record explaining a refusal, and the
    revocation plus SESSION_EXPIRED record written when an idle session is retired.
    A blanket rollback on every exception would discard both, leaving a log that
    records successes and stays silent about refusals.

    Every other exception still rolls back.
    """
    session = request.app.state.db.session_factory()
    try:
        yield session
        session.commit()
    except (RedirectRequired, AccessDenied):
        session.commit()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


@dataclass
class AuthContext:
    """The authenticated caller, their session, and the CSRF token for their forms."""

    user: User
    session: UserSession
    settings: Settings

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token

    @property
    def role(self) -> Role:
        return self.user.role_enum

    def visible_modules(self) -> list[Module]:
        """Modules to show in navigation.

        Admins see every module because they may reach every module. For a non admin
        this is exactly their grants. Navigation follows authorization; it does not
        define it.
        """
        if self.role.is_admin:
            return list(Module)
        return [m for m in Module if m in self.user.granted_modules]


def _current_auth(request: Request, db: Session, settings: Settings) -> AuthContext | None:
    """Resolve the session, enforcing idle expiry. None if not authenticated."""
    token = request.cookies.get(SESSION_COOKIE)
    user_session = load_session(db, token)
    if user_session is None:
        return None

    if user_session.is_idle_expired(settings.session_timeout_minutes):
        revoke(user_session)
        audit.record(
            db,
            action=AuditAction.SESSION_EXPIRED,
            actor=user_session.user,
            target_type="user_session",
            target_id=user_session.id,
            request=request,
            detail={"idle_timeout_minutes": settings.session_timeout_minutes},
        )
        return None

    user = user_session.user
    if not user.is_active:
        # Deactivation revokes sessions, so this is a belt and braces check.
        revoke(user_session)
        return None

    touch(user_session)
    request.state.auth = AuthContext(user=user, session=user_session, settings=settings)
    return request.state.auth


def optional_user(request: Request, db: DbSession, settings: SettingsDep) -> AuthContext | None:
    return _current_auth(request, db, settings)


def require_user(request: Request, db: DbSession, settings: SettingsDep) -> AuthContext:
    """Authenticated, active, and not overdue for a password change."""
    auth = _current_auth(request, db, settings)
    if auth is None:
        raise RedirectRequired(LOGIN_PATH)

    if auth.user.must_change_password and request.url.path != CHANGE_PASSWORD_PATH:
        raise RedirectRequired(CHANGE_PASSWORD_PATH)

    return auth


def require_authenticated_allow_password_change(
    request: Request, db: DbSession, settings: SettingsDep
) -> AuthContext:
    """Authenticated, but does not bounce a user who owes a password change.

    Used only by the change password page itself, which would otherwise redirect to
    itself forever.
    """
    auth = _current_auth(request, db, settings)
    if auth is None:
        raise RedirectRequired(LOGIN_PATH)
    return auth


CurrentUser = Annotated[AuthContext, Depends(require_user)]
OptionalUser = Annotated[AuthContext | None, Depends(optional_user)]


def require_admin(request: Request, auth: CurrentUser, db: DbSession) -> AuthContext:
    if not auth.role.is_admin:
        audit.record(
            db,
            action=AuditAction.ACCESS_DENIED,
            result=AuditResult.FAILURE,
            actor=auth.user,
            request=request,
            detail={"path": request.url.path, "reason": "admin_required"},
        )
        raise AccessDenied("This area is restricted to administrators.")
    return auth


AdminUser = Annotated[AuthContext, Depends(require_admin)]


def require_module(module: Module) -> Callable[..., AuthContext]:
    """Dependency factory gating a route on a module grant.

    An admin without the grant is allowed through, and that is recorded as emergency
    access with its own action type, so break glass use is separable from routine use
    when the log is reviewed (SECURITY.md section 3.5).
    """

    def dependency(request: Request, auth: CurrentUser, db: DbSession) -> AuthContext:
        allowed, is_emergency = auth.user.can_view(module)

        if not allowed:
            audit.record(
                db,
                action=AuditAction.ACCESS_DENIED,
                result=AuditResult.FAILURE,
                actor=auth.user,
                target_type="module",
                target_id=module.value,
                request=request,
                detail={"path": request.url.path},
            )
            raise AccessDenied(
                f"You do not have access to the {module.label} module. "
                "An administrator can grant it."
            )

        if is_emergency:
            audit.record(
                db,
                action=AuditAction.EMERGENCY_ACCESS,
                actor=auth.user,
                target_type="module",
                target_id=module.value,
                request=request,
                detail={"path": request.url.path, "reason": "admin_without_grant"},
            )

        if module.shows_patient_identity:
            # Every read of a patient level view is logged, with the filter parameters
            # used and never the rows returned (SECURITY.md section 4).
            audit.record(
                db,
                action=AuditAction.PHI_VIEW,
                actor=auth.user,
                target_type="module",
                target_id=module.value,
                request=request,
                detail={
                    "path": request.url.path,
                    "filters": dict(request.query_params),
                },
            )

        return auth

    return dependency


def require_utilization_writer(request: Request, auth: CurrentUser, db: DbSession) -> AuthContext:
    """Managers and admins may enter utilization data and notes. Viewers may not."""
    if not auth.role.can_write_utilization:
        audit.record(
            db,
            action=AuditAction.ACCESS_DENIED,
            result=AuditResult.FAILURE,
            actor=auth.user,
            request=request,
            detail={"path": request.url.path, "reason": "write_requires_manager"},
        )
        raise AccessDenied("Your role is read only for this module.")
    return auth
