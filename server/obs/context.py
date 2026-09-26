"""Observability context — correlation ids + a structured-event helper.

contextvars carry session_id / request_id through async call chains so every log
record can be stamped with them by ContextFilter (see jsonlog.py). PROC_EPOCH is
fixed at process start, so log lines from before vs after a deploy restart are
trivially distinguishable.
"""
from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from contextvars import ContextVar
from typing import Any, Iterator

session_id_var: ContextVar[str | None] = ContextVar("viberpit_session_id", default=None)
request_id_var: ContextVar[str | None] = ContextVar("viberpit_request_id", default=None)

# start-time(ms, hex) + pid: enough to tell two daemon generations apart.
PROC_EPOCH: str = f"{int(time.time() * 1000):x}-{os.getpid()}"


def new_request_id() -> str:
    """A short, log-friendly correlation id for one HTTP request."""
    return uuid.uuid4().hex[:12]


@contextlib.contextmanager
def bind_session(session_id: str | None) -> Iterator[None]:
    """Bind session_id for the block; always reset on exit (even on exception)."""
    token = session_id_var.set(session_id)
    try:
        yield
    finally:
        session_id_var.reset(token)


@contextlib.contextmanager
def bind_request(request_id: str | None) -> Iterator[None]:
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


def emit_event(
    logger: logging.Logger,
    level: int,
    event: str,
    fields: dict[str, Any],
    *,
    exc_info: bool | BaseException | None = None,
) -> None:
    """The structured-record shape, with `fields` as an explicit mapping.

    `log_event` is the ergonomic front door and covers almost every caller; use
    this one when the fields already exist as a dict, or when a traceback has to
    ride along. It stays a separate function rather than a keyword-only argument
    on `log_event` because several callers pass `log_event(..., **some_dict)`,
    and any named keyword-only parameter turns every one of those unpacks into a
    type error — mypy cannot prove the dict has no key of that name.
    """
    logger.log(level, event, extra={"event": event, "fields": fields}, exc_info=exc_info)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit a structured breadcrumb.

    `event` is a dotted name (e.g. "session.start") used as the message so
    plain-text/journald readers see it too; **fields become structured JSON keys
    via JSONFormatter.
    """
    emit_event(logger, level, event, fields)
