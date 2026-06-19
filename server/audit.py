"""Audit logging + credential redaction (T030, T025).

record_audit() — write an AuditEvent to the DB after scrubbing detail.
RedactingFilter — logging.Filter that strips token-like material from log records.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import AuditEvent

# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------

_REDACT_PATTERNS: list[re.Pattern[str]] = [
    # Anthropic API keys:  sk-ant-api03-...  sk-ant-...
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    # ccswitch envelope-encrypted token prefix
    re.compile(r"ccenc1:[A-Za-z0-9+/=]+"),
    # Generic long base64 blobs (≥40 chars of base64 alphabet) that are likely tokens
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),
]

_REDACTED = "[REDACTED]"


def _redact(text: str) -> str:
    """Return text with all token-like material replaced by [REDACTED]."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def redact_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact string values in a detail dict."""
    result: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, str):
            result[key] = _redact(value)
        elif isinstance(value, dict):
            result[key] = redact_detail(value)
        elif isinstance(value, list):
            result[key] = [
                _redact(v) if isinstance(v, str) else v for v in value
            ]
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Audit write
# ---------------------------------------------------------------------------


async def record_audit(
    db: AsyncSession,
    type: str,
    identity: str,
    detail: dict[str, Any],
) -> AuditEvent:
    """Write an AuditEvent after redacting credential material from detail.

    The AuditEvent is added to the session; the caller is responsible for commit.
    """
    clean_detail = redact_detail(detail)
    event = AuditEvent(
        id=str(uuid.uuid4()),
        at=datetime.utcnow(),
        type=type,
        identity=_redact(identity),
        detail=clean_detail,
    )
    db.add(event)
    return event


# ---------------------------------------------------------------------------
# Logging filter
# ---------------------------------------------------------------------------


class RedactingFilter(logging.Filter):
    """Logging filter that redacts token-like strings from log records.

    Install on the root logger (or specific handlers) via install_redacting_filter().
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _redact(v) if isinstance(v, str) else v for v in record.args
                )
        return True


def install_redacting_filter() -> None:
    """Install RedactingFilter on the root logger."""
    root = logging.getLogger()
    # Avoid duplicate installs
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter())
