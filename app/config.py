"""Application configuration.

Every secret is read from the environment. Nothing has a usable default. A missing
secret is a startup failure with a named error, never a silent fallback, because a
PHI application that boots without its encryption key is worse than one that refuses
to boot at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


class ConfigError(RuntimeError):
    """Raised at startup when required configuration is missing or unusable."""


# Secrets that must be present before the app will serve a request. Kept as a
# module level tuple so the startup check and the docs cannot drift apart.
REQUIRED_SECRETS = (
    "SESSION_SECRET_KEY",
    "DATABASE_ENCRYPTION_KEY",
)

# Required only once auth exists (Phase 1). Listed here so the seeding code has one
# place to look, and so README and .env.example stay in sync with the code.
SEED_ADMIN_VARS = ("ADMIN_EMAIL", "ADMIN_INITIAL_PASSWORD")

MIN_SESSION_SECRET_BYTES = 32


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    return value or None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Resolved application settings.

    Frozen so that nothing mutates configuration at runtime. Secret values live on
    this object but are never included in its repr (see __repr__ below).
    """

    environment: str
    debug: bool

    session_secret_key: str
    database_encryption_key: str
    database_path: str

    # Google Sheets ingestion. Absent is tolerated at boot in development so the app
    # can run before credentials exist; a sync attempt without it fails loudly.
    google_service_account_json: str | None

    # Defaults for the config table, seeded on first run and editable by an admin.
    session_timeout_minutes: int
    session_warning_minutes: int
    benefits_session_threshold: int
    week_start_day: str
    timezone: str

    # Feature flags.
    room_utilization_enabled: bool
    patient_funnel_enabled: bool

    _secret_fields: tuple[str, ...] = field(
        default=(
            "session_secret_key",
            "database_encryption_key",
            "google_service_account_json",
        ),
        repr=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Never let a settings object leak a secret into a log line or a traceback.
        shown = {
            "environment": self.environment,
            "debug": self.debug,
            "database_path": self.database_path,
            "session_timeout_minutes": self.session_timeout_minutes,
        }
        body = ", ".join(f"{k}={v!r}" for k, v in shown.items())
        return f"Settings({body}, secrets=REDACTED)"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def has_google_credentials(self) -> bool:
        return self.google_service_account_json is not None


def load_settings() -> Settings:
    """Build Settings from the environment, raising ConfigError on anything unusable."""
    environment = (_env("ENVIRONMENT") or "development").lower()
    if environment not in {"development", "test", "production"}:
        raise ConfigError(
            f"ENVIRONMENT must be development, test, or production, got {environment!r}"
        )

    missing = [name for name in REQUIRED_SECRETS if _env(name) is None]
    if missing:
        raise ConfigError(
            "Missing required secrets: "
            + ", ".join(missing)
            + ". Set them in Replit Secrets (see README). "
            "The application will not start without them."
        )

    session_secret = _env("SESSION_SECRET_KEY")
    assert session_secret is not None  # guaranteed by the check above
    if len(session_secret) < MIN_SESSION_SECRET_BYTES:
        raise ConfigError(
            f"SESSION_SECRET_KEY must be at least {MIN_SESSION_SECRET_BYTES} characters. "
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )

    encryption_key = _env("DATABASE_ENCRYPTION_KEY")
    assert encryption_key is not None

    debug = _env_bool("DEBUG", default=False)
    if debug and environment == "production":
        # Debug output can carry bound SQL parameters and stack frames, both of which
        # can carry PHI. It is not permitted where real data lives.
        raise ConfigError("DEBUG must not be enabled when ENVIRONMENT is production")

    week_start_day = (_env("WEEK_START_DAY") or "monday").lower()
    if week_start_day not in {"monday", "sunday"}:
        raise ConfigError("WEEK_START_DAY must be monday or sunday")

    timeout = _env_int("SESSION_TIMEOUT_MINUTES", 15)
    warning = _env_int("SESSION_WARNING_MINUTES", 13)
    if warning >= timeout:
        raise ConfigError("SESSION_WARNING_MINUTES must be less than SESSION_TIMEOUT_MINUTES")

    return Settings(
        environment=environment,
        debug=debug,
        session_secret_key=session_secret,
        database_encryption_key=encryption_key,
        database_path=_env("DATABASE_PATH") or "data/sri_dashboard.db",
        google_service_account_json=_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
        session_timeout_minutes=timeout,
        session_warning_minutes=warning,
        benefits_session_threshold=_env_int("BENEFITS_SESSION_THRESHOLD", 25),
        week_start_day=week_start_day,
        timezone=_env("APP_TIMEZONE") or "America/New_York",
        room_utilization_enabled=_env_bool("FEATURE_ROOM_UTILIZATION", default=False),
        patient_funnel_enabled=_env_bool("FEATURE_PATIENT_FUNNEL", default=False),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Used as a FastAPI dependency."""
    return load_settings()
