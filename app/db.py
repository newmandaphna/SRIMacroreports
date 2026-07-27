"""Database engine and session management.

SQLite encrypted with SQLCipher. The encryption key is applied as PRAGMA key on every
new connection, before any other statement runs on it.

Fail loud, never fall back. If the SQLCipher driver is unavailable, or the key is
missing, or the key does not open the file, the process raises rather than degrading
to an unencrypted SQLite database. See SECURITY.md section 5.2.

Everything below goes through SQLAlchemy, so moving to PostgreSQL later is a
connection configuration change rather than a rewrite.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import ConfigError, Settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import availability is environment dependent
    import sqlcipher3 as _sqlcipher

    SQLCIPHER_AVAILABLE = True
    SQLCIPHER_IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover
    _sqlcipher = None  # type: ignore[assignment]
    SQLCIPHER_AVAILABLE = False
    SQLCIPHER_IMPORT_ERROR = str(exc)


class Base(DeclarativeBase):
    """Declarative base for every model. Alembic autogenerate targets its metadata."""


def _require_sqlcipher() -> None:
    if not SQLCIPHER_AVAILABLE:
        raise ConfigError(
            "SQLCipher driver is not available, so the database cannot be encrypted "
            f"at rest ({SQLCIPHER_IMPORT_ERROR}). This application holds PHI and will "
            "not run against an unencrypted database. Install sqlcipher3-binary."
        )


def _connection_factory(settings: Settings) -> Any:
    """Build a DBAPI connect callable that keys each new connection."""
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    key = settings.database_encryption_key

    def connect() -> Any:
        conn = _sqlcipher.connect(str(db_path), check_same_thread=False)
        # PRAGMA key must be the first statement on the connection. Quote by doubling
        # single quotes; the key comes from the environment, not from user input, but
        # a key containing a quote should not produce a confusing syntax error.
        escaped = key.replace("'", "''")
        conn.execute(f"PRAGMA key = '{escaped}'")
        return conn

    return connect


def create_db_engine(settings: Settings) -> Engine:
    """Create the encrypted engine and verify the key actually opens the database."""
    _require_sqlcipher()

    engine = create_engine(
        "sqlite://",
        module=_sqlcipher,
        creator=_connection_factory(settings),
        future=True,
        # echo is never enabled: it prints bound parameters, which can carry PHI.
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.close()

    _verify_encryption(engine, settings)
    return engine


def _verify_encryption(engine: Engine, settings: Settings) -> None:
    """Prove the key works and the file is genuinely a SQLCipher database.

    A wrong key against an existing encrypted file raises on first read. An
    unencrypted file opened with a key also raises. Either way we find out at
    startup rather than at first query.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT count(*) FROM sqlite_master"))
            cipher_version = conn.exec_driver_sql("PRAGMA cipher_version").scalar()
    except Exception as exc:
        raise ConfigError(
            "Could not open the database with DATABASE_ENCRYPTION_KEY. Either the key "
            "is wrong or the file at DATABASE_PATH is not a SQLCipher database. "
            f"Underlying error: {type(exc).__name__}"
        ) from exc

    if not cipher_version:
        raise ConfigError(
            "The database driver reported no cipher version, which means the file is "
            "not encrypted. Refusing to serve PHI from an unencrypted database."
        )

    logger.info("Database ready at %s (SQLCipher %s)", settings.database_path, cipher_version)


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
