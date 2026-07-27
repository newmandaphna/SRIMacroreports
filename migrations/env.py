"""Alembic environment.

Builds its engine from DATABASE_URL (Replit's built-in PostgreSQL). The URL is
a runtime-managed environment variable and is never written into alembic.ini.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

# Importing the models package registers every table on Base.metadata so that
# autogenerate can see them.
import app.models  # noqa: F401
from app.db import Base

config = context.config
target_metadata = Base.metadata


def _get_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. In Replit this is provided automatically "
            "by the built-in PostgreSQL database."
        )
    return url


def run_migrations_offline() -> None:
    """Offline mode: emit SQL to stdout for review."""
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_get_url(), future=True)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
