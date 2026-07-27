"""Database engine and session management.

Uses Replit's built-in PostgreSQL via the DATABASE_URL environment variable.
SQLAlchemy manages connection pooling. The DatabaseHandle interface is unchanged
from the SQLCipher version so all callers continue to work without modification.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import ConfigError, Settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for every model. Alembic autogenerate targets its metadata."""


def create_db_engine(settings: Settings) -> Engine:
    """Create a PostgreSQL engine and verify connectivity."""
    engine = create_engine(
        settings.database_url,
        future=True,
        # echo is never enabled: it prints bound parameters, which can carry PHI.
        echo=False,
        pool_pre_ping=True,
    )
    _verify_connectivity(engine)
    return engine


def _verify_connectivity(engine: Engine) -> None:
    """Prove the database is reachable before the app starts serving requests."""
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar() or "PostgreSQL"
    except Exception as exc:
        raise ConfigError(
            "Could not connect to the PostgreSQL database. "
            "Ensure DATABASE_URL is set and the database is reachable. "
            f"Underlying error: {type(exc).__name__}: {exc}"
        ) from exc

    logger.info("Database ready (%s)", version.split(",")[0])


class DatabaseHandle:
    """Holds the engine and session factory for the application lifetime."""

    def __init__(self, settings: Settings) -> None:
        self.engine = create_db_engine(settings)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
