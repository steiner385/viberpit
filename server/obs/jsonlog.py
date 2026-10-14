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
    """Suppress asyncio.CancelledError pool records from the sqlalchemy.pool tree.

    During shutdown or request cancellation SQLAlchemy logs CancelledError at
    ERROR level from pool checkin/reset/close. These are expected and not
    actionable; suppressing them keeps the ERROR band (and the error sink, which
    auto-files issues) high signal.

    Installed on the root HANDLERS, not the ``sqlalchemy.pool`` logger: a logger's
    filters only run for records created on that exact logger, and SQLAlchemy
    logs from children like ``sqlalchemy.pool.impl.AsyncAdaptedQueuePool`` whose
    records reach the root handlers without passing a parent's filters (#2678).
    Hence the explicit logger-name scope — a handler sees every record.
    """

    _PREFIX = "sqlalchemy.pool"

    def filter(self, record: logging.LogRecord) -> bool:
        if not (record.name == self._PREFIX or record.name.startswith(self._PREFIX + ".")):
            return True
        if record.levelno >= logging.ERROR and record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
                return False
        return True


def install_pool_cancelled_filter() -> None:
    """Attach ``_PoolCancelledFilter`` to every root handler. Idempotent.

    Handler-level because a filter on the ``sqlalchemy.pool`` logger never sees
    records from its child loggers (#2678) — see the class docstring.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _PoolCancelledFilter) for f in handler.filters):
            handler.addFilter(_PoolCancelledFilter())


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


class _QueuedFileHandler(logging.handlers.QueueHandler):
    """Hand records to a ``QueueListener`` thread that owns the rotating file (#2356).

    ``RotatingFileHandler.emit`` stats the file (``shouldRollover`` → ``os.path.exists``)
    and writes it on the CALLER's thread, so any ``log.*`` on the event loop blocked the
    loop for as long as the log dir's filesystem was slow — two of the #1913 stall dumps
    were exactly that frame. Here the caller only formats (JSON + redaction, pure CPU)
    and enqueues; every filesystem call happens on the listener thread.

    Formatting stays on the caller's side on purpose: ``prepare()`` drops ``exc_info``,
    so the traceback must be rendered into the JSON ``exc`` field before the hand-off.
    The file handler behind the queue therefore writes the pre-rendered line verbatim.
    ``close()`` stops the listener (draining the queue) and closes the file, so a caller
    that just closes handlers it added — tests, atexit — leaves no thread behind.
    """

    def __init__(self, target: logging.Handler) -> None:
        import queue
        q: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
        super().__init__(q)
        self.target = target
        target.setFormatter(logging.Formatter("%(message)s"))
        listener = logging.handlers.QueueListener(q, target)
        listener.start()
        self._listener: logging.handlers.QueueListener | None = listener

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()
            self.target.close()
        super().close()


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
    # #831: httpx logs one INFO line per request, and the edge's daemon RPC client
    # polls every few seconds -- two thirds of the edge's journal volume was
    # "HTTP Request: POST .../rpc/..." lines. Failures surface through the callers'
    # own logging; LOG_LEVEL=DEBUG still shows the per-request lines.
    if root.level > logging.DEBUG:
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

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
            rotating = logging.handlers.RotatingFileHandler(
                # Unique per (host, process) — see log_file_name's docstring.
                Path(log_dir) / log_file_name(PROCESS),
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            # The file I/O runs on a listener thread, never the event loop (#2356).
            # The context filter and formatter sit on the queue side: contextvars
            # are only readable on the thread that logged.
            file_handler = _QueuedFileHandler(rotating)
            file_handler.setFormatter(fmt)
            file_handler.addFilter(ctx)
            root.addHandler(file_handler)
            import atexit
            atexit.register(file_handler.close)
        except OSError as exc:
            root.warning("jsonlog: log dir %s unwritable (%s); stdout-only", log_dir, exc)

    install_redacting_filter()

    # Error-triage capture (spec 2026-09-07 §4): WARNING+ records → error_signatures.
    # Installed here so the edge and the daemon get it from the same call; the
    # flusher is started by each process's lifespan once the DB is up.
    if bool(getattr(settings, "error_sink_enabled", True)):
        from server.obs.errsink import install_error_sink
        install_error_sink(source=process)

    # Suppress CancelledError pool noise from sqlalchemy.pool (expected during
    # shutdown/cancellation; ~400 per rotation at ERROR level — not actionable).
    # After the error sink so it covers that handler too.
    install_pool_cancelled_filter()

    _configured = True
