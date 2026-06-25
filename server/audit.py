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
    # Generic long base64 blobs that are clearly padded secrets (≥60 chars + mandatory
    # padding `=`).  Requiring at least one `=` pad character avoids false-positive
    # matches on unpadded hex git SHAs, long identifiers, and minified code segments
    # while still catching OAuth refresh tokens and other high-entropy base64 payloads.
    # NOTE: sk-ant-* and ccenc1:* are caught by the explicit patterns above, so there
    # is no regression risk in tightening this rule.
    #
    # The leading `(?<![A-Za-z0-9+/])` anchors the match to the START of a base64 run.
    # Without it, an UNPADDED run of N base64 chars is O(N^2): the engine retries the
    # `{60,}` greedy match at every one of the N start positions (each scanning the
    # whole remaining run before failing on the missing `=`). A ~200KB base64 image
    # data URI in a transcript event then pinned the single-threaded event loop for
    # MINUTES when redacted during WS attach replay, hanging the whole server. The
    # negative lookbehind leaves exactly one valid start per run → linear time.
    re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{60,}={1,2}"),
    # itsdangerous-signed session cookie value:  <payload>.<timestamp>.<signature>
    # Shape: base64url payload (≥16 chars) · base64url ts (≥6 chars) · sig (≥20 chars)
    re.compile(r"\b[\w-]{16,}\.[\w-]{6,}\.[\w-]{20,}\b"),
]

_REDACTED = "[REDACTED]"


def _redact(text: str) -> str:
    """Return text with all token-like material replaced by [REDACTED]."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def redact_text(text: str) -> str:
    """Public credential redactor for arbitrary text (e.g. serialized JSON logs)."""
    return _redact(text)


def _redact_value(value: Any) -> Any:
    """Redact a single value (str, dict, list, or passthrough for other types)."""
    if isinstance(value, str):
        return _redact(value)
    elif isinstance(value, dict):
        return redact_detail(value)
    elif isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def redact_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact string values in a detail dict (including nested lists of dicts)."""
    return {key: _redact_value(value) for key, value in detail.items()}


def redact_event(wire: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact secret-shaped strings in an outbound WS event dict.

    Reuses redact_detail which now recurses into both nested dicts and lists,
    so this covers: plain string fields, nested dicts, and lists of events
    (as found in ``{"type": "batch", "events": [...]}``) uniformly.
    """
    return redact_detail(wire)


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
    """Install RedactingFilter on the root logger AND each of its handlers.

    A filter on a Logger is only consulted for records emitted at that logger;
    records propagated up from child loggers (e.g. ``server.*``) reach the root's
    *handlers* without re-applying the root logger's filters. So we must also add
    the filter to every root handler for redaction to cover module logging.
    Idempotent — safe to call again after the logging stack (uvicorn) is set up.
    """
    def _has(flist) -> bool:
        return any(isinstance(f, RedactingFilter) for f in flist)

    root = logging.getLogger()
    if not _has(root.filters):
        root.addFilter(RedactingFilter())
    if not root.handlers:
        # Ensure at least one handler carries the filter.
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        if not _has(handler.filters):
            handler.addFilter(RedactingFilter())
