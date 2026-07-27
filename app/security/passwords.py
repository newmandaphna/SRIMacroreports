"""Password hashing and policy.

Argon2id via passlib. No reversible storage, no MD5 or SHA family.

Policy, per the build specification and current NIST guidance:
  minimum 12 characters
  checked against a common password list
  no forced rotation, because rotation drives users toward predictable increments
"""

from __future__ import annotations

import re
import unicodedata

from passlib.context import CryptContext

from app.security.common_passwords import COMMON_PASSWORDS

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256  # bound the work an unauthenticated caller can ask for

_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__type="ID",
    argon2__memory_cost=65536,  # 64 MiB
    argon2__time_cost=3,
    argon2__parallelism=4,
)


class PasswordPolicyError(ValueError):
    """Raised when a proposed password fails policy. Message is safe to show a user."""


def hash_password(password: str) -> str:
    return _context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password. Never raises on a malformed hash, just returns False."""
    try:
        return _context.verify(password, password_hash)
    except Exception:
        return False


def needs_rehash(password_hash: str) -> bool:
    return _context.needs_update(password_hash)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def validate_password(password: str, *, email: str = "", display_name: str = "") -> None:
    """Raise PasswordPolicyError if the password is unacceptable.

    The email and display name are checked against because a password built from the
    account it protects is guessable by anyone who knows the account.
    """
    password = _normalize(password)

    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    if password.strip() != password:
        raise PasswordPolicyError("Password must not start or end with a space.")

    lowered = password.lower()

    if lowered in COMMON_PASSWORDS:
        raise PasswordPolicyError(
            "That password is on a list of commonly used passwords. Choose another."
        )

    # A common password with digits bolted on either end is still a common password.
    stripped = re.sub(r"^\W*|[\W\d]*$", "", lowered)
    if stripped and stripped in COMMON_PASSWORDS:
        raise PasswordPolicyError(
            "That password is a common password with characters added. Choose another."
        )

    if _contains_account_identifier(lowered, email, display_name):
        raise PasswordPolicyError("Password must not contain your email address or your name.")

    if len(set(password)) < 5:
        raise PasswordPolicyError("Password must not be mostly the same character.")

    if _is_single_repeated_run(lowered):
        raise PasswordPolicyError("Password must not be a repeated sequence.")


def _contains_account_identifier(lowered: str, email: str, display_name: str) -> bool:
    candidates: list[str] = []
    if email:
        local = email.lower().split("@", 1)[0]
        candidates.append(local)
        candidates.append(email.lower())
    if display_name:
        candidates.extend(part.lower() for part in re.split(r"\s+", display_name) if part)

    return any(len(c) >= 4 and c in lowered for c in candidates)


def _is_single_repeated_run(lowered: str) -> bool:
    """True for things like 'abcabcabcabc' or 'aaaaaaaaaaaa'."""
    for unit in range(1, len(lowered) // 2 + 1):
        if len(lowered) % unit == 0 and lowered[:unit] * (len(lowered) // unit) == lowered:
            return True
    return False
