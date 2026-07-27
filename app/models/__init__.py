"""Model package.

Importing this package must register every model on Base.metadata, because Alembic
autogenerate reads that metadata. Add each new module to the import list below.

Phase 2 adds: therapists, sessions, data_sources, sync_runs, import_errors, config,
lookups.
"""

from app.db import Base
from app.models.audit import AuditLog, AuditLogImmutableError
from app.models.enums import AuditAction, AuditResult, Module, Role
from app.models.session import UserSession
from app.models.user import ModuleGrant, User

__all__ = [
    "AuditAction",
    "AuditLog",
    "AuditLogImmutableError",
    "AuditResult",
    "Base",
    "Module",
    "ModuleGrant",
    "Role",
    "User",
    "UserSession",
]
