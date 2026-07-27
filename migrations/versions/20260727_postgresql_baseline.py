"""PostgreSQL baseline: all tables from scratch.

Replaces the two SQLite/SQLCipher migrations. No batch mode, no PRAGMA,
standard PostgreSQL column types throughout.

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-07-27 17:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.models.types  # noqa: F401  -- makes UTCDateTime/Money importable by path

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", app.models.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("created_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", app.models.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", app.models.types.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------------------
    # audit_log
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_label", sa.String(length=320), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("result", sa.String(length=10), nullable=False),
        sa.Column("target_type", sa.String(length=60), nullable=True),
        sa.Column("target_id", sa.String(length=60), nullable=True),
        sa.Column("source_ip", sa.String(length=45), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"], unique=False)
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"], unique=False)
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"], unique=False)

    # ------------------------------------------------------------------
    # module_grants
    # ------------------------------------------------------------------
    op.create_table(
        "module_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("module", sa.String(length=40), nullable=False),
        sa.Column("granted_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("granted_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["granted_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "module", name="uq_grant_user_module"),
    )
    op.create_index("ix_module_grants_user_id", "module_grants", ["user_id"], unique=False)

    # ------------------------------------------------------------------
    # user_sessions
    # ------------------------------------------------------------------
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", app.models.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("source_ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("is_admin_elevated", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=True)
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"], unique=False)

    # ------------------------------------------------------------------
    # therapists
    # ------------------------------------------------------------------
    op.create_table(
        "therapists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column(
            "employment_type",
            sa.Enum(
                "salaried_benefits",
                "percentage_legacy",
                "other",
                name="employmenttype",
                native_enum=False,
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("display_name"),
    )

    # ------------------------------------------------------------------
    # app_config
    # ------------------------------------------------------------------
    op.create_table(
        "app_config",
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("key"),
    )

    # ------------------------------------------------------------------
    # data_sources
    # ------------------------------------------------------------------
    op.create_table(
        "data_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("google_sheets", "demo", name="sourceprovider", native_enum=False, length=30),
            nullable=False,
        ),
        sa.Column("spreadsheet_id", sa.String(length=120), nullable=True),
        sa.Column("spreadsheet_url", sa.Text(), nullable=True),
        sa.Column("tab_name", sa.String(length=200), nullable=True),
        sa.Column("header_row", sa.Integer(), nullable=False),
        sa.Column("column_mapping", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=True),
        sa.Column("coverage_end", sa.Date(), nullable=True),
        sa.Column("last_synced_at", app.models.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("created_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("label"),
    )

    # ------------------------------------------------------------------
    # therapist_aliases
    # ------------------------------------------------------------------
    op.create_table(
        "therapist_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("therapist_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=120), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "sheet_config_tab",
                "observed",
                "manual",
                name="aliassource",
                native_enum=False,
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("created_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["therapist_id"], ["therapists.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alias", name="uq_therapist_alias"),
    )
    op.create_index(
        "ix_therapist_aliases_therapist_id", "therapist_aliases", ["therapist_id"], unique=False
    )

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------
    op.create_table(
        "lookups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "insurance", "location", "note", name="lookupkind", native_enum=False, length=20
            ),
            nullable=False,
        ),
        sa.Column("long_name", sa.String(length=200), nullable=False),
        sa.Column("short_code", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("imported_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lookups_kind", "lookups", ["kind"], unique=False)
    op.create_index("ix_lookups_short_code", "lookups", ["short_code"], unique=False)

    # ------------------------------------------------------------------
    # sync_runs
    # ------------------------------------------------------------------
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column(
            "mode",
            sa.Enum("dry_run", "live", name="syncmode", native_enum=False, length=20),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running", "success", "failed", name="syncstatus", native_enum=False, length=20
            ),
            nullable=False,
        ),
        sa.Column("started_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("finished_at", app.models.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("rows_read", sa.Integer(), nullable=False),
        sa.Column("rows_inserted", sa.Integer(), nullable=False),
        sa.Column("rows_updated", sa.Integer(), nullable=False),
        sa.Column("rows_unchanged", sa.Integer(), nullable=False),
        sa.Column("rows_rejected", sa.Integer(), nullable=False),
        sa.Column("date_min", sa.Date(), nullable=True),
        sa.Column("date_max", sa.Date(), nullable=True),
        sa.Column("unmapped_columns", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("run_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["run_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_runs_source_id", "sync_runs", ["source_id"], unique=False)

    # ------------------------------------------------------------------
    # import_errors
    # ------------------------------------------------------------------
    op.create_table(
        "import_errors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_row_ref", sa.String(length=40), nullable=True),
        sa.Column(
            "reason",
            sa.Enum(
                "missing_therapist",
                "unknown_therapist",
                "missing_patient_name",
                "missing_dos",
                "bad_date",
                "bad_money",
                "missing_cpt",
                "duplicate_key",
                name="rejectreason",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("field", sa.String(length=60), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("patient_hint", sa.String(length=120), nullable=True),
        sa.Column("therapist_hint", sa.String(length=120), nullable=True),
        sa.Column("created_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", app.models.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_id", sa.Integer(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["resolved_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_errors_reason", "import_errors", ["reason"], unique=False)
    op.create_index("ix_import_errors_source_id", "import_errors", ["source_id"], unique=False)
    op.create_index("ix_import_errors_sync_run_id", "import_errors", ["sync_run_id"], unique=False)

    # ------------------------------------------------------------------
    # sessions (visits)
    # ------------------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_row_ref", sa.String(length=40), nullable=True),
        sa.Column("therapist_id", sa.Integer(), nullable=False),
        sa.Column("patient_name", sa.String(length=200), nullable=False),
        sa.Column("patient_name_normalized", sa.String(length=200), nullable=False),
        sa.Column("patient_code", sa.String(length=40), nullable=True),
        sa.Column("dos", sa.Date(), nullable=False),
        sa.Column("cpt", sa.String(length=40), nullable=False),
        sa.Column("cpt_base", sa.String(length=40), nullable=False),
        sa.Column("insurance_short", sa.String(length=40), nullable=True),
        sa.Column("location_short", sa.String(length=40), nullable=True),
        sa.Column("note_code", sa.String(length=20), nullable=True),
        sa.Column("recorded_flag", sa.String(length=40), nullable=True),
        sa.Column("due_from_pt", app.models.types.Money(), nullable=False),
        sa.Column("paid_by_pt", app.models.types.Money(), nullable=False),
        sa.Column("pt_amount_due", app.models.types.Money(), nullable=False),
        sa.Column("due_from_ins", app.models.types.Money(), nullable=False),
        sa.Column("paid_by_ins", app.models.types.Money(), nullable=False),
        sa.Column("ins_balance", app.models.types.Money(), nullable=False),
        sa.Column("total_due", app.models.types.Money(), nullable=False),
        sa.Column("total_paid", app.models.types.Money(), nullable=False),
        sa.Column("total_balance", app.models.types.Money(), nullable=False),
        sa.Column("imported_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.models.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("last_sync_run_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["last_sync_run_id"], ["sync_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["therapist_id"], ["therapists.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "therapist_id",
            "patient_name_normalized",
            "dos",
            "cpt",
            name="uq_visit_identity",
        ),
    )
    op.create_index("ix_sessions_insurance_short", "sessions", ["insurance_short"], unique=False)
    op.create_index("ix_sessions_location_short", "sessions", ["location_short"], unique=False)
    op.create_index("ix_sessions_patient_code", "sessions", ["patient_code"], unique=False)
    op.create_index("ix_sessions_source_id", "sessions", ["source_id"], unique=False)
    op.create_index("ix_sessions_therapist_id", "sessions", ["therapist_id"], unique=False)
    op.create_index("ix_visit_cpt_base", "sessions", ["cpt_base"], unique=False)
    op.create_index("ix_visit_dos", "sessions", ["dos"], unique=False)
    op.create_index("ix_visit_therapist_dos", "sessions", ["therapist_id", "dos"], unique=False)


def downgrade() -> None:
    op.drop_table("sessions")
    op.drop_table("import_errors")
    op.drop_table("sync_runs")
    op.drop_table("lookups")
    op.drop_table("therapist_aliases")
    op.drop_table("data_sources")
    op.drop_table("app_config")
    op.drop_table("therapists")
    op.drop_table("user_sessions")
    op.drop_table("module_grants")
    op.drop_table("audit_log")
    op.drop_table("users")
