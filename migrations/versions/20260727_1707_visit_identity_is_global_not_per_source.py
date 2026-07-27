"""visit identity is global not per source

Removes source_id from the visit upsert key, so one visit is one row whichever
quarterly sheet delivered it. See ASSUMPTIONS.md A-022.

Any database written before this point can hold the duplicates the old key allowed,
and the new constraint cannot be created while they are there, so they are collapsed
first. Nothing references sessions.id, so deleting the surplus rows is safe.

Revision ID: 33b222d203da
Revises: 451550904b21
Create Date: 2026-07-27 17:07:20.865433+00:00
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders custom column types by their full dotted path, so the module
# that defines them has to be importable here (app.models.types.UTCDateTime).
import app.models.types  # noqa: F401

revision: str = "33b222d203da"
down_revision: str | None = "451550904b21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

IDENTITY = ("therapist_id", "patient_name_normalized", "dos", "cpt")

# Keeps the highest id in each group, which is the most recently imported copy, and
# deletes the rest. Grouping on the same columns as the new constraint means the
# survivors satisfy it by construction.
COLLAPSE = sa.text(
    """
    DELETE FROM sessions
    WHERE id NOT IN (
        SELECT MAX(id) FROM sessions
        GROUP BY therapist_id, patient_name_normalized, dos, cpt
    )
    """
)


def upgrade() -> None:
    connection = op.get_bind()
    removed = connection.execute(COLLAPSE).rowcount
    if removed:
        # Worth saying out loud: the figures on every page change when this runs.
        logger.warning(
            "collapsed %s duplicate visit rows that the old per source key allowed", removed
        )

    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_constraint("uq_visit_identity", type_="unique")
        batch_op.create_unique_constraint("uq_visit_identity", list(IDENTITY))


def downgrade() -> None:
    # The rows this migration collapsed are not recoverable, and they should not be:
    # each one was a second copy of a visit already present. Re-syncing every source
    # after a downgrade restores the per source duplicates if they are wanted.
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_constraint("uq_visit_identity", type_="unique")
        batch_op.create_unique_constraint("uq_visit_identity", ["source_id", *IDENTITY])
