"""Seed the first administrator from the environment.

Runs at startup, once. If an active admin already exists it does nothing, so a restart
never resurrects or overwrites an account.

Also syncs the seeded admin's password hash if ADMIN_PASSWORD is set and the stored
hash no longer matches — this covers the common case where the secret value changes
(or the secret was renamed) between deployments and the database hash falls out of sync.

The seeded password must be changed on first login before any other route is reachable
(SECURITY.md section 3.2).
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import ConfigError, Settings
from app.models.enums import AuditAction, Module, Role
from app.models.user import ModuleGrant, User
from app.security import audit
from app.security.passwords import (
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    hash_password,
    validate_password,
    verify_password,
)

logger = logging.getLogger(__name__)


def sync_admin_password(db: Session) -> None:
    """Re-hash the seeded admin's password if ADMIN_PASSWORD has changed.

    This is a startup self-heal: if the secret value is updated (or the secret
    was renamed from ADMIN_INITIAL_PASSWORD to ADMIN_PASSWORD) the stored hash
    falls out of sync and every login attempt returns 401.  Running this at boot
    keeps the hash current without requiring a manual database edit.

    Only touches the account that matches ADMIN_EMAIL.  Does nothing if
    ADMIN_PASSWORD is unset, if the hash already matches, or if the account
    has ever had a successful login (must_change_password == False means the
    user set their own password and we must not overwrite it).
    """
    password = os.environ.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_INITIAL_PASSWORD") or ""
    email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    if not password or not email:
        return

    admin = db.execute(
        select(User).where(User.email == email, User.role == Role.ADMIN)
    ).scalar_one_or_none()
    if admin is None:
        return

    if verify_password(password, admin.password_hash):
        return  # already in sync — nothing to do

    # Hash is out of sync with ADMIN_PASSWORD (secret was renamed, value was changed,
    # or the database was re-created). Reset it and force a password change so the
    # admin sets a permanent credential on next login.
    admin.password_hash = hash_password(password)
    admin.must_change_password = True
    logger.warning(
        "ADMIN_PASSWORD does not match stored hash for %s — hash updated at startup "
        "and must_change_password reset. Log in with ADMIN_PASSWORD then set a new "
        "permanent password.",
        email,
    )


def seed_admin(db: Session, settings: Settings) -> User | None:
    """Create the initial admin if there is not already one. Returns it if created."""
    existing = db.execute(
        select(func.count(User.id)).where(User.role == Role.ADMIN, User.is_active.is_(True))
    ).scalar_one()
    if existing:
        return None

    email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    # Accept either name; ADMIN_PASSWORD is the current name in Replit Secrets.
    password = os.environ.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_INITIAL_PASSWORD") or ""

    if not email or not password:
        # Not fatal: a fresh checkout should be able to boot and show a clear message
        # rather than crash. But it is loud, because nobody can sign in until it is
        # fixed, and a PHI application with no way in is not a working application.
        logger.error(
            "No administrator exists and ADMIN_EMAIL / ADMIN_PASSWORD are not "
            "set. Nobody can sign in. Set both in Replit Secrets and restart."
        )
        return None

    if "@" not in email:
        raise ConfigError(f"ADMIN_EMAIL is not a valid email address: {email!r}")

    try:
        validate_password(password, email=email)
    except PasswordPolicyError as exc:
        raise ConfigError(
            f"ADMIN_PASSWORD does not meet the password policy: {exc} "
            f"(minimum {MIN_PASSWORD_LENGTH} characters, not a common password)."
        ) from exc

    admin = User(
        email=email,
        display_name="Administrator",
        role=Role.ADMIN,
        password_hash=hash_password(password),
        # Forced change on first login. The value in the environment is a bootstrap
        # credential, not a permanent one.
        must_change_password=True,
        is_active=True,
    )
    db.add(admin)
    db.flush()

    # The seed admin gets every module explicitly, so their normal work does not show
    # up in the log as emergency access.
    for module in Module:
        db.add(ModuleGrant(user_id=admin.id, module=module, granted_by_id=admin.id))

    audit.record(
        db,
        action=AuditAction.USER_CREATED,
        actor=admin,
        target_type="user",
        target_id=admin.id,
        detail={"seeded_from_environment": True, "role": Role.ADMIN.value},
    )

    logger.info("Seeded initial administrator. Password change is forced on first login.")
    return admin
