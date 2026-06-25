"""Structured JSON logging for ccswitch.

JSONFormatter renders each LogRecord as a single-line JSON object including
correlation fields injected by ContextFilter, structured `event`/`fields` from
log_event(), and a full traceback when exc_info is set. The serialized line is
passed through the shared credential redactor so secrets never reach a sink —
including structured fields and forwarded client logs that the record-level
RedactingFilter does not cover.

configure_logging() attaches a stdout handler (captured by journald) and a
size-rotating JSON file handler. It is idempotent and never raises: an unwritable
log dir degrades to stdout-only with a single warning.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.audit import install_redacting_filter, redact_text
from server.obs.context import PROC_EPOCH, request_id_var, session_id_var
from server.time_iso import iso_utc

_MAX_BYTES = 50 * 1024 * 1024
_BACKUP_COUNT = 10


def _coerce(value: Any) -> Any:
    """Best-effort JSON-safe coercion; never raises."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    try:
        return repr(value)
    except Exception:  # noqa: BLE001 — repr must never break logging
        return "<unrepr>"


class ContextFilter(logging.Filter):
    """Stamp correlation ids from contextvars onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = session_id_var.get()
        record.request_id = request_id_var.get()
        record.proc_epoch = PROC_EPOCH
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": iso_utc(datetime.fromtimestamp(record.created, tz=timezone.utc)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
            "proc_epoch": getattr(record, "proc_epoch", PROC_EPOCH),
            "session_id": getattr(record, "session_id", None),
            "request_id": getattr(record, "request_id", None),
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = event
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            payload["fields"] = {str(k): _coerce(v) for k, v in fields.items()}
        source = getattr(record, "source", None)
        if source is not None:
            payload["source"] = source
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        line = json.dumps(payload, default=_coerce, ensure_ascii=False)
        return redact_text(line)


_configured = False


def configure_logging() -> None:
    """Attach JSON stdout + rotating-file handlers to the root logger. Idempotent.

    Never raises: a bad log dir degrades to stdout-only with one warning.
    """
    global _configured
    if _configured:
        return

    from server.config import get_settings

    settings = get_settings()
    level_name = str(getattr(settings, "log_level", "INFO") or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    fmt = JSONFormatter()
    ctx = ContextFilter()

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.addFilter(ctx)
    root.addHandler(stream)

    log_dir = str(getattr(settings, "log_dir", "") or "")
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                Path(log_dir) / "ccswitch.jsonl",
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            file_handler.addFilter(ctx)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("ccswitch: log dir %s unwritable (%s); stdout-only", log_dir, exc)

    install_redacting_filter()
    _configured = True
