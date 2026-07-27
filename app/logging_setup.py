"""Logging configuration with a PHI scrubbing filter.

This is a backstop, not a licence. The primary control is not passing PHI to a log
call in the first place (see SECURITY.md section 6.4). The filter exists because a
single careless f-string in an exception handler should not put a patient name into
a log file that gets shipped somewhere.

The filter runs on the root logger, installed before any application logger is
created, so it also catches records emitted by third party libraries.
"""

from __future__ import annotations

import logging
import re
import sys

REDACTED = "[REDACTED]"

# Keys whose values must never appear in a log line, matched inside dict reprs,
# JSON fragments, and key=value pairs.
_SENSITIVE_KEYS = (
    "patient_name",
    "patient name",
    "patientname",
    "patient_code",
    "patient code",
    "patientcode",
    "birthdate",
    "birth_date",
    "dob",
    "home_email",
    "homeemail",
    "work_email",
    "workemail",
    "email",
    "phone",
    "zip",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "session_secret_key",
    "database_encryption_key",
    "google_service_account_json",
)

_KEY_ALTERNATION = "|".join(re.escape(k) for k in _SENSITIVE_KEYS)

# key="value" / key='value' / key=value / "key": "value"
_KV_PATTERN = re.compile(
    rf"""(?ix)
    (?P<key>['"]?\b(?:{_KEY_ALTERNATION})\b['"]?\s*[:=]\s*)
    (?P<value>"[^"]*"|'[^']*'|[^\s,;)}}\]]+)
    """
)

# The Q sheet's composite match key, THERAPIST|Patient name|date|CPT, which carries a
# patient name in its second field and shows up in raw row diagnostics.
_COMPOSITE_KEY_PATTERN = re.compile(
    r"\b[A-Z][A-Z \-]{2,}\|[^|]{2,60}\|\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\|[A-Za-z0-9 .\-]{2,20}\b"
)

# "Surname,Given" as the Q sheet writes patient names. Deliberately narrow: it needs a
# comma with no space after it, which is the sheet's format and not ordinary prose.
_SURNAME_COMMA_PATTERN = re.compile(r"\b[A-Z][a-zA-Z'\-]{1,30},[A-Z][a-zA-Z'\-. ]{1,30}\b")

_PATTERNS: tuple[re.Pattern[str], ...] = (
    _COMPOSITE_KEY_PATTERN,
    _SURNAME_COMMA_PATTERN,
)


def scrub(text: str) -> str:
    """Redact PHI and secret bearing substrings from a string."""
    if not text:
        return text
    text = _KV_PATTERN.sub(lambda m: f"{m.group('key')}{REDACTED}", text)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class PHIScrubbingFilter(logging.Filter):
    """Scrub the rendered message, the args, and any exception text on a record.

    Returns True always: the record is never dropped, only cleaned. Dropping records
    would create gaps in an operational log, which is its own problem.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - malformed record, keep it moving
            rendered = str(record.msg)

        cleaned = scrub(rendered)
        if cleaned != rendered or record.args:
            # Collapse to a single pre rendered message so the formatter cannot
            # re-expand the original args and undo the scrub.
            record.msg = cleaned
            record.args = ()

        if record.exc_text:
            record.exc_text = scrub(record.exc_text)

        return True


class _ScrubbingFormatter(logging.Formatter):
    """Formatter that scrubs the final output, including formatted tracebacks.

    exc_info is rendered by the formatter, after filters have run, so a traceback
    carrying a patient name in a local variable repr would otherwise slip past the
    filter. This is the last gate before the stream.
    """

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def configure_logging(level: str = "INFO") -> None:
    """Install the scrubbing filter and formatter on the root logger.

    Call this once, as early as possible, before any other logger is used.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _ScrubbingFormatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(PHIScrubbingFilter())
    root.addHandler(handler)

    # SQLAlchemy echo prints bound parameters, which is a direct PHI to log path.
    # Pin it off regardless of what any library default or env var tries to do.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
