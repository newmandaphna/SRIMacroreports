"""Alembic environment.

Builds its engine from application settings rather than from a URL in alembic.ini,
because the database is SQLCipher encrypted and the key must never be written into a
config file in the repository.
"""

from __future__ import annotations

from alembic import context

# Importing the models package registers every table on Base.metadata so that
# autogenerate can see them. It is empty until Phase 2.
import app.models  # noqa: F401
from app.config import load_settings
from app.db import Base, create_db_engine

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline mode is not supported: there is no URL to render against.

    An encrypted database cannot be migrated by emitting SQL to a file, since the
    file would then need the key to be applied by hand.
    """
    raise RuntimeError(
        "Offline migrations are not supported for the encrypted database. "
        "Run alembic without --sql."
    )


def run_migrations_online() -> None:
    settings = load_settings()
    engine = create_db_engine(settings)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites the table.
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
