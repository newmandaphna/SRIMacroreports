"""the psychiatrists are SCHOR and YEO

Revision ID: c1d2e3f4a5b6
Revises: fe6a0bc9d6dc
Create Date: 2026-07-29 04:00:00.000000+00:00

The previous migration tagged the practice's psychiatrists by alias, but with the
names misspelled (YOE, SHOR), so it matched nobody. The roster's own spellings are
YEO (Hyung Yeo, MD) and SCHOR (Robin Schor, MD). Applied migrations are never
edited, so this one repeats the update with the right names. Idempotent, and the
field stays editable in the admin, so a value an admin has since set by hand is
simply set again to the same thing or corrected.

Production note: the parent migration (fe6a0bc9d6dc) landed in alembic_version on
the production database but its DDL (ADD COLUMN discipline) was never committed,
leaving the column absent. This migration recovers from that by adding the column
idempotently before issuing the UPDATE.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "fe6a0bc9d6dc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Ensure the column exists before touching it.  In a healthy database that has
    # already run fe6a0bc9d6dc successfully, ADD COLUMN IF NOT EXISTS is a no-op.
    # On the production database where fe6a0bc9d6dc's DDL never committed, this
    # creates the column so the UPDATE below can proceed.
    op.execute(
        """
        ALTER TABLE therapists
            ADD COLUMN IF NOT EXISTS discipline     VARCHAR(20) NOT NULL DEFAULT 'therapist',
            ADD COLUMN IF NOT EXISTS weekly_expected_sessions INTEGER
        """
    )

    op.execute(
        """
        UPDATE therapists SET discipline = 'psychiatrist'
        WHERE id IN (
            SELECT therapist_id FROM therapist_aliases
            WHERE upper(alias) IN ('YEO', 'SCHOR')
        )
        OR upper(display_name) IN ('YEO', 'SCHOR', 'HYUNG YEO', 'ROBIN SCHOR')
        """
    )


def downgrade() -> None:
    # Deliberately nothing: reverting a data correction would reinstate a mistake.
    pass
