"""sync runs carry typed errors and reconciliation

error_kind and error_detail are the machine readable half of a failure, so the
code can tell a rate limit from a renamed header without string matching the
prose. reconciliation records what a live run changed against what was already
stored in its own date span. All three are nullable: rows from before this
revision genuinely carry none of it.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-31 17:20:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Use IF NOT EXISTS so the migration is idempotent against the partial-commit
    # failure mode where the DDL lands in production but alembic_version does not.
    op.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS error_kind VARCHAR(40)")
    op.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS error_detail JSON")
    op.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS reconciliation JSON")


def downgrade() -> None:
    op.drop_column("sync_runs", "reconciliation")
    op.drop_column("sync_runs", "error_detail")
    op.drop_column("sync_runs", "error_kind")
