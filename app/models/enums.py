"""Roles, module grants, and audit actions.

Two orthogonal axes, per the build specification:
  Role   controls what you may DO.
  Grant  controls what you may SEE.

Financial access does not confer patient level access. `patient_funnel` is a separate
grant that must be given explicitly.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    VIEWER = "viewer"

    @property
    def label(self) -> str:
        return {
            Role.ADMIN: "Administrator",
            Role.MANAGER: "Manager",
            Role.VIEWER: "Viewer",
        }[self]

    @property
    def description(self) -> str:
        return {
            Role.ADMIN: (
                "Everything: user administration, configuration, data sources, sync, "
                "and the audit log."
            ),
            Role.MANAGER: "Read granted modules, and enter utilization data and notes.",
            Role.VIEWER: "Read granted modules.",
        }[self]

    @property
    def can_write_utilization(self) -> bool:
        return self in (Role.ADMIN, Role.MANAGER)

    @property
    def is_admin(self) -> bool:
        return self is Role.ADMIN


class Module(StrEnum):
    FINANCIAL = "financial"
    THERAPIST_UTILIZATION = "therapist_utilization"
    ROOM_UTILIZATION = "room_utilization"
    PATIENT_FUNNEL = "patient_funnel"

    @property
    def label(self) -> str:
        return {
            Module.FINANCIAL: "Financial",
            Module.THERAPIST_UTILIZATION: "Therapist utilization",
            Module.ROOM_UTILIZATION: "Room utilization",
            Module.PATIENT_FUNNEL: "Patient funnel",
        }[self]

    @property
    def shows_patient_identity(self) -> bool:
        """True for modules that can display patient name or code.

        Only the patient funnel does. Every other module is aggregate only, and its
        queries never select a patient column. See SECURITY.md section 6.3.
        """
        return self is Module.PATIENT_FUNNEL


class AuditAction(StrEnum):
    """Every auditable action. Adding one here is how it becomes loggable."""

    # Authentication
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    SESSION_EXPIRED = "session_expired"
    SESSION_EXTENDED = "session_extended"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 - an action name, not a secret
    ACCOUNT_LOCKED = "account_locked"

    # User administration
    USER_CREATED = "user_created"
    USER_UPDATED = "user_updated"
    USER_DEACTIVATED = "user_deactivated"
    USER_REACTIVATED = "user_reactivated"
    USER_PASSWORD_RESET = "user_password_reset"  # noqa: S105 - an action name
    GRANT_ADDED = "grant_added"
    GRANT_REMOVED = "grant_removed"
    ROLE_CHANGED = "role_changed"

    # Configuration and data
    CONFIG_CHANGED = "config_changed"
    DATA_SOURCE_CHANGED = "data_source_changed"
    SYNC_RUN = "sync_run"
    MANUAL_EDIT = "manual_edit"

    # Access to protected information
    PHI_VIEW = "phi_view"
    EXPORT = "export"
    EMERGENCY_ACCESS = "emergency_access"
    ACCESS_DENIED = "access_denied"

    # Administration of the log itself (reads only; there is no write path)
    AUDIT_VIEWED = "audit_viewed"


class AuditResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
