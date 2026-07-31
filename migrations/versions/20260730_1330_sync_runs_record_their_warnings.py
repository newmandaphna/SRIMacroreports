"""sync runs record their warnings

A run can now succeed while carrying news the admin must see, such as a mapped
descriptive column that has vanished from the sheet. The warning has to survive
navigation the same way unmapped_columns does, or it is a flash message wearing
a seatbelt: gone the moment the page changes, and the drift becomes permanent.

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-07-30 13:30:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default so the rows already in the table read as "no warnings", which
    # is the truth about them: nothing was recorded at the time.
    op.add_column(
        "sync_runs",
        sa.Column("warnings", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )


def downgrade() -> None:
    op.drop_column("sync_runs", "warnings")
