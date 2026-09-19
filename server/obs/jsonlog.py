"""Structured JSON logging for ViberPit.

JSONFormatter renders each LogRecord as a single-line JSON object including
correlation fields injected by ContextFilter, structured `event`/`fields` from
log_event(), and a full traceback when exc_info is set. The serialized line is
passed through the shared credential redactor so secrets never reach a sink —
including structured fields and forwarded client logs that the record-level
RedactingFilter does not cover.

configure_logging() attaches a stdout handler (captured by journald) and a
size-rotating JSON file handler. It is idempotent and never raises: an unwritable
log dir — or one that is not provably host-local (#1010) — degrades to
stdout-only with a single warning.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.audit import install_redacting_filter, redact_text
from server.obs.context import PROC_EPOCH, request_id_var, session_id_var
from server.obs.logdir import is_shared_dir, log_file_name
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


class _PoolCancelledFilter(logging.Filter):
    """Suppress asyncio.CancelledError pool-reset records from sqlalchemy.pool.

    During shutdown or request cancellation SQLAlchemy logs CancelledError at
    ERROR level from pool checkin/reset. These are expected and not actionable;
    suppressing them at the source logger keeps ERROR-band signal-to-noise high.
    Returning False stops propagation before any handler sees the record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
                return False
        return True


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
# Which process this is ("edge" | "daemon"); set by configure_logging(). The error
# sink (errsink.py) tags every captured signature with it.
PROCESS = "edge"


def configure_logging(*, process: str = "edge") -> None:
    """Attach JSON stdout + rotating-file handlers to the root logger. Idempotent.

    ``process`` names the caller ("edge" for server/app.py, "daemon" for
    server/ipc/serve.py), is exposed as ``PROCESS``, and names the file so the two
    never share one rotating handle. Never raises: a bad log dir degrades to
    stdout-only with one warning.
    """
    global _configured, PROCESS
    if _configured:
        return
    PROCESS = process

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
    if log_dir and is_shared_dir(log_dir):
        # The default is per-host by construction (server/obs/logdir.py), so reaching
        # here means an operator pointed the log dir at a share. Two hosts
        # appending to — and independently rotating — one file over FUSE is #1010;
        # the stdout handler above already puts every line in this host's journal,
        # so dropping only the file sink loses nothing that was safe to keep.
        root.warning(
            "jsonlog: log dir %s is not provably host-local; stdout-only "
            "(two hosts rotating one file over a share is #1010)", log_dir)
        log_dir = ""
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                # Unique per (host, process) — see log_file_name's docstring.
                Path(log_dir) / log_file_name(PROCESS),
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            file_handler.addFilter(ctx)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("jsonlog: log dir %s unwritable (%s); stdout-only", log_dir, exc)

    install_redacting_filter()
    # Suppress CancelledError pool-reset noise from sqlalchemy.pool (expected during
    # shutdown/cancellation; ~400 per rotation at ERROR level — not actionable).
    logging.getLogger("sqlalchemy.pool").addFilter(_PoolCancelledFilter())

    # Error-triage capture (spec 2026-09-07 §4): WARNING+ records → error_signatures.
    # Installed here so the edge and the daemon get it from the same call; the
    # flusher is started by each process's lifespan once the DB is up.
    if bool(getattr(settings, "error_sink_enabled", True)):
        from server.obs.errsink import install_error_sink
        install_error_sink(source=process)

    _configured = True
