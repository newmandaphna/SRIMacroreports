"""Model package.

Empty until Phase 2, when the data model lands (therapists, sessions, data_sources,
sync_runs, import_errors, config, lookups) alongside the Phase 1 tables (users, roles,
module grants, sessions, audit_log).

Importing this package must register every model on Base.metadata, because Alembic
autogenerate reads that metadata. Add each new module to the import list below.
"""

from app.db import Base

__all__ = ["Base"]
