"""Pure signature extraction for the error sink (error-triage spec §5).

A *signature* is the stable identity of one kind of log record: what stays the
same across occurrences (logger, event or normalized message, exception class,
innermost app frame) hashed into a 16-hex fingerprint, plus the volatile parts
kept as context. No I/O; the sink (errsink.py) owns persistence.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import traceback
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from server.audit import redact_text
from viberpit import brand

SAMPLE_MAX = 4000
_NORM_MAX = 200
_TITLE_SUMMARY_MAX = 100
OWN_LOGGER = "server.obs.errsink"
# What makes a traceback frame "ours": the server package, or the core package under
# `src/`. The core marker is anchored on `/src/` on purpose — a bare `/<slug>/` would
# also match every dependency frame under a checkout DIRECTORY named after the
# product (`…/viberpit/.venv/lib/…/sqlalchemy/…`), and the innermost of those would
# be reported as the app frame instead of the project's own.
_APP_PATH_MARKERS = ("/server/", f"/src/{brand.SLUG}/")

# Order matters: uuid before hex (a uuid contains hex runs), quoted strings before
# numbers, id-like path segments before bare hex/numbers so "/535694fe" → "/<id>",
# and 0x-addresses before the number masker so one does not render "<n>x7f9c".
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_ID_SEGMENT = re.compile(r"/[0-9a-z_.-]*\d[0-9a-z_.-]*(?=/|\s|$)", re.I)
_HEX_ADDR = re.compile(r"\b0x[0-9a-f]+\b", re.I)
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
# A measured value: digits, any fractional groups, whatever unit follows.
#
# This was `\b\d+\b`, which masks a number only when a word boundary closes it —
# so "0.78s" kept its fraction ("<n>.78s") and "61s" was not masked at all, the
# trailing "s" being a word character. Every distinct measurement therefore minted
# its own fingerprint, and one loop_lag stall was filed as eight auto-errors
# (#1848, #1850, #1705, #1770, #1771, #1774, #1789, #1797). The lookbehind keeps
# the old rule that digits *inside* a word belong to the word ("sqlite3", "p95");
# the greedy digit run supplies the trailing edge that `\b` could not.
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)*")
_WS = re.compile(r"\s+")

_CLIENT_EXC = re.compile(r"^\s*([A-Za-z_]\w*(?:Error|Exception))\b")
_CLIENT_FRAME = re.compile(r"^\s*at\s+(?:(?P<fn>[^\s(]+)\s+\()?(?P<file>[^\s()]+?):\d+:\d+\)?\s*$")
_ASSET_HASH = re.compile(r"[-.][0-9A-Za-z_]{6,}(?=\.m?js$)")


def normalize(text: str) -> str:
    """Lowercase and replace ids, hex runs, numbers and quoted strings with placeholders."""
    t = text.lower()
    t = _UUID.sub("<id>", t)
    t = _QUOTED.sub("<str>", t)
    t = _ID_SEGMENT.sub("/<id>", t)
    t = _HEX_ADDR.sub("<hex>", t)
    t = _HEX.sub("<hex>", t)
    t = _NUM.sub("<n>", t)
    t = _WS.sub(" ", t).strip()
    return t[:_NORM_MAX]


def app_frame(exc_info: Any) -> str | None:
    """``module:func`` of the innermost traceback frame inside this project."""
    if not exc_info or len(exc_info) < 3 or exc_info[2] is None:
        return None
    chosen = None
    for fr in traceback.extract_tb(exc_info[2]):
        if any(m in fr.filename for m in _APP_PATH_MARKERS):
            chosen = fr
    if chosen is None:
        return None
    return f"{PurePosixPath(chosen.filename).stem}:{chosen.name}"


def client_frame(stack: str) -> tuple[str | None, str | None]:
    """(exception name, 'basename:function') from a browser stack; hash-stripped."""
    if not stack:
        return None, None
    lines = stack.splitlines()
    exc = None
    m = _CLIENT_EXC.match(lines[0]) if lines else None
    if m:
        exc = m.group(1)
    for line in lines[1:]:
        fm = _CLIENT_FRAME.match(line)
        if not fm:
            continue
        raw = fm.group("file").split("?")[0]
        base = PurePosixPath(raw).name or raw
        base = _ASSET_HASH.sub("", base)
        fn = fm.group("fn") or "<anonymous>"
        return exc, f"{base}:{fn}"
    return exc, None


@dataclass(frozen=True)
class Signature:
    fingerprint: str
    source: str                 # edge | daemon | client
    level: str
    logger: str
    event: str | None
    title: str
    sample_message: str
    sample_exc: str | None
    context: dict[str, Any]
    session_id: str | None


def _fp(parts: tuple[str, ...]) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def _clip(text: str | None, cap: int = SAMPLE_MAX) -> str | None:
    if text is None:
        return None
    return redact_text(text)[:cap]


def _fields_summary(fields: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in fields.items())


def signature_of(record: logging.LogRecord, *, source: str, host: str) -> Signature | None:
    """The signature for *record*, or None when the record must not be counted."""
    if record.levelno < logging.WARNING or record.name == OWN_LOGGER:
        return None
    if getattr(record, "source", None) == "frontend":
        return None  # the bare re-emit in client_logs.py; the event record counts
    event = getattr(record, "event", None)
    fields = getattr(record, "fields", None)
    fields = dict(fields) if isinstance(fields, dict) else {}
    level = record.levelname
    session_id = getattr(record, "session_id", None)
    base_ctx: dict[str, Any] = {
        "request_id": getattr(record, "request_id", None),
        "proc_epoch": getattr(record, "proc_epoch", None),
        "host": host,
    }

    if record.name == "client" and event == "client.error":
        msg = str(fields.get("msg") or "")
        stack = str(fields.get("stack") or "")
        exc_name, frame = client_frame(stack)
        summary = normalize(msg)
        parts: tuple[str, ...] = ("client", level, summary, exc_name or "", frame or "")
        return Signature(
            fingerprint=_fp(parts), source="client", level=level, logger="client",
            event=event, title=f"{level} client: {summary[:_TITLE_SUMMARY_MAX]}",
            sample_message=_clip(msg) or "", sample_exc=_clip(stack) or None,
            context={**base_ctx, "app_version": fields.get("app_version"),
                     "url": fields.get("url")},
            session_id=session_id,
        )

    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 — a bad format string must not break capture
        message = str(record.msg)
    exc_info = record.exc_info if record.exc_info else None
    exc_name = exc_info[0].__name__ if exc_info and exc_info[0] else ""
    frame = app_frame(exc_info) or f"{record.module}:{record.funcName}"
    key = event if event else normalize(message)
    summary = normalize(_fields_summary(fields)) if event else key
    parts = (source, level, record.name, key, exc_name, frame)
    sample = message if not event else f"{event} {json.dumps(fields, default=str)}"
    sample_exc = "".join(traceback.format_exception(*exc_info)) if exc_info else None
    return Signature(
        fingerprint=_fp(parts), source=source, level=level, logger=record.name,
        event=event, title=f"{level} {event or record.name}: {summary[:_TITLE_SUMMARY_MAX]}",
        sample_message=_clip(sample) or "", sample_exc=_clip(sample_exc),
        context=base_ctx, session_id=session_id,
    )
