"""Writing to the audit log.

One entry point, `record`, so that every audit write goes through the same scrubbing
and the same shape. The log is append only; see app/models/audit.py for the guards
that make that true rather than aspirational.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.logging_setup import scrub
from app.models.audit import AuditLog
from app.models.enums import AuditAction, AuditResult
from app.models.user import User

MAX_DETAIL_CHARS = 2000


def client_ip(request: Request | None) -> str | None:
    """Best effort client address.

    Behind a proxy, X-Forwarded-For's first entry is the client. This is for the audit
    trail only and is never used for authorization, so a spoofed header misleads a
    reader of the log but cannot grant access.
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host[:45] if request.client else None


def _detail_to_text(detail: dict[str, Any] | str | None) -> str | None:
    if detail is None:
        return None
    text = detail if isinstance(detail, str) else json.dumps(detail, sort_keys=True, default=str)
    # Backstop. Callers must not put PHI in detail in the first place, but a filter
    # parameter that happens to carry a patient name should not survive to the table.
    return scrub(text)[:MAX_DETAIL_CHARS]


def record(
    session: Session,
    *,
    action: AuditAction,
    result: AuditResult = AuditResult.SUCCESS,
    actor: User | None = None,
    actor_label: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    request: Request | None = None,
    detail: dict[str, Any] | str | None = None,
) -> AuditLog:
    """Append one audit record.

    actor_label carries the attempted identifier when there is no resolved actor, so a
    failed login for an unknown address is still attributable to what was typed.
    """
    if actor is None and actor_label is None:
        actor_label = "anonymous"

    entry = AuditLog(
        actor_id=actor.id if actor else None,
        actor_label=(actor.email if actor else actor_label) or "anonymous",
        action=action,
        result=result,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        source_ip=client_ip(request),
        detail=_detail_to_text(detail),
    )
    session.add(entry)
    return entry
