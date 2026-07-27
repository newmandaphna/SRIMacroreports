"""Model package.

Importing this package must register every model on Base.metadata, because Alembic
autogenerate reads that metadata. Add each new module to the import list below.
"""

from app.db import Base
from app.models.audit import AuditLog, AuditLogImmutableError
from app.models.data_source import (
    AppConfig,
    DataSource,
    ImportError,
    Lookup,
    LookupKind,
    RejectReason,
    SourceProvider,
    SyncMode,
    SyncRun,
    SyncStatus,
)
from app.models.enums import AuditAction, AuditResult, Module, Role
from app.models.session import UserSession
from app.models.therapist import (
    AliasSource,
    EmploymentType,
    Therapist,
    TherapistAlias,
)
from app.models.user import ModuleGrant, User
from app.models.utilization import UtilizationNote
from app.models.visit import Visit

__all__ = [
    "AliasSource",
    "AppConfig",
    "AuditAction",
    "AuditLog",
    "AuditLogImmutableError",
    "AuditResult",
    "Base",
    "DataSource",
    "EmploymentType",
    "ImportError",
    "Lookup",
    "LookupKind",
    "Module",
    "ModuleGrant",
    "RejectReason",
    "Role",
    "SourceProvider",
    "SyncMode",
    "SyncRun",
    "SyncStatus",
    "Therapist",
    "TherapistAlias",
    "User",
    "UserSession",
    "UtilizationNote",
    "Visit",
]
