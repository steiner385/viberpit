# Error Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture every WARNING+ log record from the edge, the session daemon and the browser into deduplicated `error_signatures` rows, and file each signature that crosses a threshold as exactly one GitHub issue in `steiner385/viberpit`.

**Architecture:** A `logging.Handler` (`server/obs/errsink.py`) aggregates records by a stable fingerprint (`server/obs/error_signature.py`) and a 5 s flusher upserts them to a new table. An `error-triage` automation (`server/automations/error_triage.py`) ticks on the primary daemon, applies a pure filing policy, and drives GitHub through a new shared file/comment/regress helper (`server/automations/issue_filing.py`) that the CI/CD Doctor (#1433) and the session-feedback filer (#1434) will later adopt.

**Tech Stack:** Python 3.12, stdlib `logging`, SQLAlchemy async (SQLite in tests, Postgres in prod), FastAPI lifespan, `gh` CLI via `ccswitch.gitrepos._run_gh`, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-07-error-triage-design.md`

## Global Constraints

- Every new column is **nullable**; every timestamp column is **naive UTC** (`DateTime` without `timezone=True`). Never `func.now()`; Python-side now is `naive_utc_now()` added to `server/db/clock.py` in Task 3.
- Migration `0034_error_signatures` chains `down_revision = "0033_triage_items"`.
- No `except (asyncio.CancelledError, Exception)` and no `suppress(CancelledError…)` anywhere (blocked by `test_no_swallowed_outer_cancellation.py`). Teardown uses `server/aio.py:cancel_and_reap`.
- The sink never blocks, never raises into a logging call site, ignores its own logger `server.obs.errsink`, and holds at most `MAX_PENDING = 512` fingerprints between flushes.
- Fingerprint = first 16 hex of sha1 over `"\x1f"`-joined parts (spec §5). Hashed: source, level, logger, event-or-normalized-message, exception class, innermost app frame (client: top frame as hash-stripped basename:function). Never hashed: timestamps, ids, proc_epoch, host, app_version, line numbers.
- Issue label is `auto-error` plus `source:client` or `source:server`. Never apply the Backlog Runner's eligible label.
- Dedupe rules (spec §6.1): open issue → comment at most once per `comment_every_h`; closed `completed` → new `[regression]` issue linking the old one, never reopen; closed `not_planned` → muted; `daily_cap` new issues per UTC day.
- Kill switch `CCSWITCH_ERROR_SINK_ENABLED` (default true) in **both** `Settings` definitions in `server/config.py` and in `_MANAGED` in `server/api/settings.py`.
- Bodies: blockquoted under an "untrusted" header, `<!--`/`-->` neutralized, `redact_text`, capped at `ci_issues.BODY_MAX_CHARS`.
- Run the four-step gate before pushing: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Use the main checkout's `.venv`; unset `CCSWITCH_SESSIOND_*` and `CCSWITCH_DAEMON_ROLE` first (see CLAUDE.md and the memory note about `env -i` on rocky).
- Commit each task; end every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; write messages to a file and pass `-F`.

## File structure

| File | Responsibility |
|---|---|
| `server/obs/error_signature.py` (new) | Pure: `normalize`, `signature_of(record, *, source, host) -> Signature | None`, frame parsing. No I/O. |
| `server/obs/errsink.py` (new) | `ErrorSink(logging.Handler)`: aggregate, bounded, flusher task, DB upsert. `install_error_sink`, `get_error_sink`, `reset_for_tests`. |
| `server/obs/jsonlog.py` | `configure_logging(*, process="edge")` gains the `process` kwarg and installs the sink when enabled. |
| `server/obs/metrics.py` | `error_sink_dropped_total` counter. |
| `server/ipc/serve.py` | Daemon calls `configure_logging(process="daemon")` (#1423); starts/stops the flusher in `daemon_lifespan`. |
| `server/app.py` | Edge starts/stops the flusher in `_lifespan`. |
| `server/config.py`, `server/api/settings.py` | `error_sink_enabled` flag. |
| `server/db/clock.py` | `naive_utc_now()` Python-side helper. |
| `server/db/models.py`, `server/db/migrations/versions/0034_error_signatures.py` | `ErrorSignature` model + migration record. |
| `server/github/ci_issues.py` | `comment_on_issue`, `issue_states` (one GraphQL query). |
| `server/automations/issue_filing.py` (new) | `Linked`, `Decision`, `Applied`, `decide`, `apply`, `neutralize_markers`. Shared by #1433/#1434 later. |
| `server/automations/error_triage.py` (new) | `ErrorTriage` automation, `should_file`, `is_ignored`, `issue_title`, `issue_body`, `ErrorTriageLoop`, `ErrorTriageActionProvider`. |
| `server/automations/registry.py`, `server/sessions/orchestrator.py` | Register + wire the tick loop, ctx, action provider. |
| Tests | `server/tests/test_error_signature.py`, `test_errsink.py`, `test_error_signature_model.py`, `test_ci_issues_comment_states.py`, `test_issue_filing.py`, `test_error_triage_policy.py`, `test_error_triage_loop.py`, `test_error_triage_actions.py`, `test_daemon_configures_logging.py`. |

Run tests from the worktree root with the main checkout's interpreter, e.g.
`PY=/home/tony/GitHub/viberpit/.venv/bin/python; $PY -m pytest server/tests/test_x.py -v`
(adjust `PY` to wherever the prebuilt `.venv` lives; `ls -d /home/tony/GitHub/*/.venv` finds it).

---

### Task 1: The daemon configures logging (#1423) and `configure_logging` learns its process

**Files:**
- Modify: `server/obs/jsonlog.py:105-149`
- Modify: `server/ipc/serve.py:292-305` (the `serve()` function)
- Test: `server/tests/test_daemon_configures_logging.py`

**Interfaces:**
- Produces: `configure_logging(*, process: str = "edge") -> None`. `process` is stored in module global `jsonlog.PROCESS` (read by Task 4 to tag the sink's source).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_daemon_configures_logging.py
"""#1423: the session daemon must configure structured logging like the edge does.
Without it the root logger has no handler, INFO is dropped and WARNING+ leaks as
bare text through logging.lastResort."""
from __future__ import annotations

import logging

from fastapi import FastAPI


def test_serve_configures_json_logging(monkeypatch, tmp_path):
    from server.ipc import serve as serve_mod
    from server.obs import jsonlog

    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(jsonlog, "_configured", False)
    monkeypatch.setattr(serve_mod, "build_daemon", lambda: (FastAPI(), object()))
    monkeypatch.setattr(serve_mod.uvicorn, "run", lambda *a, **k: None)
    try:
        serve_mod.serve(str(tmp_path / "d.sock"))
        assert any(isinstance(h.formatter, jsonlog.JSONFormatter) for h in root.handlers), \
            "daemon entrypoint did not install the JSON handler"
        assert jsonlog.PROCESS == "daemon"
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_configure_logging_defaults_to_edge(monkeypatch):
    from server.obs import jsonlog
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(jsonlog, "_configured", False)
    try:
        jsonlog.configure_logging()
        assert jsonlog.PROCESS == "edge"
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `$PY -m pytest server/tests/test_daemon_configures_logging.py -v`
Expected: FAIL — `AttributeError: module 'server.obs.jsonlog' has no attribute 'PROCESS'` (first test may also fail on the missing handler).

- [ ] **Step 3: Add the `process` kwarg to `configure_logging`**

In `server/obs/jsonlog.py`, replace the `_configured = False` line and the function signature/first lines:

```python
_configured = False
# Which process this is ("edge" | "daemon"); set by configure_logging(). The error
# sink (errsink.py) tags every captured signature with it.
PROCESS = "edge"


def configure_logging(*, process: str = "edge") -> None:
    """Attach JSON stdout + rotating-file handlers to the root logger. Idempotent.

    ``process`` names the caller ("edge" for server/app.py, "daemon" for
    server/ipc/serve.py) and is exposed as ``PROCESS``. Never raises: a bad log
    dir degrades to stdout-only with one warning.
    """
    global _configured, PROCESS
    if _configured:
        return
    PROCESS = process
```

Leave the rest of the body unchanged.

- [ ] **Step 4: Call it from the daemon entrypoint**

In `server/ipc/serve.py`, add the import near the other `server.*` imports:

```python
from server.obs.jsonlog import configure_logging
```

and in `serve()` make the first statement:

```python
def serve(socket_path: str | None = None) -> None:
    # #1423: the daemon owns the orchestrator in remote mode, so it is most of the
    # app's diagnostic surface — and it never configured logging, so every INFO
    # was dropped and WARNING+ leaked as bare text. Same call the edge makes.
    configure_logging(process="daemon")
    # Back-compat: an explicit socket_path arg forces a unix bind at that path.
    endpoint = Endpoint("unix", path=socket_path) if socket_path else sessiond_endpoint()
```

- [ ] **Step 5: Run the tests**

Run: `$PY -m pytest server/tests/test_daemon_configures_logging.py server/tests/test_ipc_entrypoint.py -v`
Expected: PASS (the entrypoint test is `slow`; it spawns the real daemon and must still answer `ping`).

- [ ] **Step 6: Commit**

```bash
git add server/obs/jsonlog.py server/ipc/serve.py server/tests/test_daemon_configures_logging.py
printf 'fix(daemon): configure structured logging in the session daemon entrypoint\n\nFixes #1423. configure_logging() gains a process kwarg so the error sink\n(next tasks) can tag which process captured a record.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git commit -F /tmp/c1.txt
```

---

### Task 2: Pure signature extraction — `server/obs/error_signature.py`

**Files:**
- Create: `server/obs/error_signature.py`
- Test: `server/tests/test_error_signature.py`

**Interfaces:**
- Produces:
  - `normalize(text: str) -> str`
  - `SAMPLE_MAX = 4000`
  - `@dataclass(frozen=True) Signature(fingerprint: str, source: str, level: str, logger: str, event: str | None, title: str, sample_message: str, sample_exc: str | None, context: dict[str, Any], session_id: str | None)`
  - `signature_of(record: logging.LogRecord, *, source: str, host: str) -> Signature | None` — `None` means "do not count this record".
  - `client_frame(stack: str) -> tuple[str | None, str | None]` → `(exception_name, "basename:function")`.
  - `app_frame(exc_info) -> str | None` → `"module:func"` of the innermost frame under `/server/` or `/ccswitch/`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_error_signature.py
from __future__ import annotations

import logging

import pytest


def _rec(msg="boom", *, level=logging.ERROR, name="server.sessions.orchestrator",
         exc_info=None, pathname="/repo/server/sessions/orchestrator.py", func="tick",
         **extra):
    rec = logging.LogRecord(name, level, pathname, 42, msg, (), exc_info, func=func)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def _sig(**kw):
    from server.obs.error_signature import signature_of
    kw.setdefault("source", "edge")
    kw.setdefault("host", "dobby")
    rec = kw.pop("rec")
    return signature_of(rec, **kw)


@pytest.mark.parametrize("raw, want", [
    ("Session 3f2a9c1e-1b2d-4e5f-8a9b-0c1d2e3f4a5b failed after 12 retries",
     "session <id> failed after <n> retries"),
    ("token abcdef0123 rejected", "token <hex> rejected"),
    ('open "/var/log/x.log" failed', "open <str> failed"),
    ("worktree /home/t/.ccswitch/worktrees/viberpit/535694fe gone",
     "worktree /home/t/.ccswitch/worktrees/viberpit/<id> gone"),
    ("a   b\n c", "a b c"),
])
def test_normalize(raw, want):
    from server.obs.error_signature import normalize
    assert normalize(raw) == want


def test_normalize_caps_length():
    from server.obs.error_signature import normalize
    assert len(normalize("x" * 1000)) == 200


def test_fingerprint_stable_across_volatile_fields():
    a = _sig(rec=_rec("db locked after 1 retries", session_id="s1", request_id="r1"))
    b = _sig(rec=_rec("db locked after 7 retries", session_id="s2", request_id="r2"))
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint
    assert len(a.fingerprint) == 16
    assert a.session_id == "s1" and b.session_id == "s2"


@pytest.mark.parametrize("change", [
    {"level": logging.WARNING},
    {"name": "server.other"},
    {"source": "daemon"},
])
def test_fingerprint_differs_on_identity_fields(change):
    base = _sig(rec=_rec("boom"))
    src = change.pop("source", "edge")
    other = _sig(rec=_rec("boom", **change), source=src)
    assert base.fingerprint != other.fingerprint


def test_event_records_hash_the_event_not_the_fields():
    a = _sig(rec=_rec("completion.question_hold", event="completion.question_hold",
                      fields={"sid": "a1", "reason": "x"}))
    b = _sig(rec=_rec("completion.question_hold", event="completion.question_hold",
                      fields={"sid": "b2", "reason": "y"}))
    assert a.fingerprint == b.fingerprint
    assert a.event == "completion.question_hold"
    assert a.title.startswith("ERROR completion.question_hold:")
    assert a.title == "ERROR completion.question_hold: sid=a1 reason=x"  # fields → title, normalized


def test_exception_class_and_app_frame_are_hashed():
    def _boom():
        raise ValueError("bad 123")
    try:
        _boom()
    except ValueError:
        import sys
        ei = sys.exc_info()
    with_exc = _sig(rec=_rec("failed", exc_info=ei))
    without = _sig(rec=_rec("failed"))
    assert with_exc.fingerprint != without.fingerprint
    assert with_exc.sample_exc and "ValueError: bad 123" in with_exc.sample_exc


def test_app_frame_prefers_innermost_project_frame():
    from server.obs.error_signature import app_frame
    import sys
    def inner():
        raise RuntimeError("x")
    try:
        inner()
    except RuntimeError:
        ei = sys.exc_info()
    frame = app_frame(ei)
    # this test file lives under server/tests → it IS a project frame
    assert frame == "test_error_signature:inner"


def test_client_frame_parses_browser_stack_and_strips_asset_hash():
    from server.obs.error_signature import client_frame
    stack = ("TypeError: Cannot read properties of undefined (reading 'id')\n"
             "    at renderRow (https://vp.example/assets/index-Bx9k2L3f.js:12:34567)\n"
             "    at Array.map (<anonymous>)")
    assert client_frame(stack) == ("TypeError", "index.js:renderRow")
    assert client_frame("") == (None, None)


def test_client_event_record_becomes_client_signature():
    rec = _rec("client.error", name="client", event="client.error",
               fields={"msg": "TypeError: x is undefined", "stack": "TypeError: x\n    at f (https://h/assets/app-abc123.js:1:2)",
                       "url": "https://vp/sessions/1", "app_version": "4d2096a5",
                       "identity": "someone@example"})
    sig = _sig(rec=rec)
    assert sig.source == "client"
    assert sig.logger == "client"
    assert sig.context["app_version"] == "4d2096a5"
    assert "identity" not in sig.context
    assert sig.sample_exc.startswith("TypeError: x")
    # line:col do not change the fingerprint
    rec2 = _rec("client.error", name="client", event="client.error",
                fields={"msg": "TypeError: x is undefined",
                        "stack": "TypeError: x\n    at f (https://h/assets/app-zzz999.js:9:99)"})
    assert _sig(rec=rec2).fingerprint == sig.fingerprint


def test_bare_frontend_reemit_is_skipped():
    rec = _rec("TypeError: x", name="client", level=logging.ERROR, source="frontend")
    assert _sig(rec=rec) is None


def test_below_warning_and_own_logger_are_skipped():
    assert _sig(rec=_rec("info", level=logging.INFO)) is None
    assert _sig(rec=_rec("x", name="server.obs.errsink")) is None


def test_sample_is_redacted_and_capped():
    sig = _sig(rec=_rec("token sk-ant-api03-" + "A" * 80 + " " + "y" * 5000))
    assert "sk-ant-api03-AAAA" not in sig.sample_message
    assert len(sig.sample_message) <= 4000
```

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_error_signature.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.obs.error_signature'`.

- [ ] **Step 3: Implement the module**

```python
# server/obs/error_signature.py
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

SAMPLE_MAX = 4000
_NORM_MAX = 200
_TITLE_SUMMARY_MAX = 100
OWN_LOGGER = "server.obs.errsink"
_APP_PATH_MARKERS = ("/server/", "/ccswitch/")

# Order matters: uuid before hex (a uuid contains hex runs), quoted strings before
# ints, id-like path segments before bare hex/ints so "/535694fe" → "/<id>".
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_ID_SEGMENT = re.compile(r"/[0-9a-z_.-]*\d[0-9a-z_.-]*(?=/|\s|$)", re.I)
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_INT = re.compile(r"\b\d+\b")
_WS = re.compile(r"\s+")

_CLIENT_EXC = re.compile(r"^\s*([A-Za-z_]\w*(?:Error|Exception))\b")
_CLIENT_FRAME = re.compile(r"^\s*at\s+(?:(?P<fn>[^\s(]+)\s+\()?(?P<file>[^\s()]+?):\d+:\d+\)?\s*$")
_ASSET_HASH = re.compile(r"[-.][0-9A-Za-z_]{6,}(?=\.m?js$)")


def normalize(text: str) -> str:
    """Lowercase and replace ids, hex runs, ints and quoted strings with placeholders."""
    t = text.lower()
    t = _UUID.sub("<id>", t)
    t = _QUOTED.sub("<str>", t)
    t = _ID_SEGMENT.sub("/<id>", t)
    t = _HEX.sub("<hex>", t)
    t = _INT.sub("<n>", t)
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
        parts = ("client", level, summary, exc_name or "", frame or "")
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
```

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest server/tests/test_error_signature.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/obs/error_signature.py server/tests/test_error_signature.py
printf 'feat(obs): pure error-signature extraction and fingerprinting\n\nSpec 2026-09-07-error-triage-design.md section 5.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git commit -F /tmp/c2.txt
```

---

### Task 3: `ErrorSignature` model, migration, `naive_utc_now`, settings flag, metric

**Files:**
- Modify: `server/db/clock.py` (append helper)
- Modify: `server/db/models.py` (after `SessionFeedback`, ~line 848)
- Create: `server/db/migrations/versions/0034_error_signatures.py`
- Modify: `server/config.py:263-270` and `:558-563`
- Modify: `server/api/settings.py:48-52` (`_MANAGED`)
- Modify: `server/obs/metrics.py` (after `session_feedback_total`, ~line 232)
- Test: `server/tests/test_error_signature_model.py`

**Interfaces:**
- Produces: `server.db.clock.naive_utc_now() -> datetime`; `server.db.models.ErrorSignature` with columns listed below; `Settings.error_sink_enabled: bool`; `server.obs.metrics.error_sink_dropped_total: Counter` (label-less).

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_error_signature_model.py
from __future__ import annotations

import importlib
from datetime import datetime

import pytest


def test_naive_utc_now_is_naive():
    from server.db.clock import naive_utc_now
    now = naive_utc_now()
    assert isinstance(now, datetime) and now.tzinfo is None


@pytest.mark.asyncio
async def test_error_signature_round_trips(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import ErrorSignature
    async with db_session() as db:
        db.add(ErrorSignature(fingerprint="abc", source="edge", level="ERROR",
                              logger="server.x", title="ERROR server.x: boom",
                              sample_message="boom", count=1, count_since_filed=1,
                              sample_context={"host": "dobby", "recent_sessions": ["s1"]}))
        await db.commit()
    async with db_session() as db:
        row = (await db.execute(select(ErrorSignature))).scalars().one()
        assert row.status == "observed"
        assert row.issue_number is None and row.muted_by is None
        assert row.sample_context["recent_sessions"] == ["s1"]
        assert row.first_seen_at is not None and row.first_seen_at.tzinfo is None


@pytest.mark.asyncio
async def test_fingerprint_is_unique(isolated_db):
    from sqlalchemy.exc import IntegrityError
    from server.db import db_session
    from server.db.models import ErrorSignature
    async with db_session() as db:
        db.add(ErrorSignature(fingerprint="dup"))
        await db.commit()
    with pytest.raises(IntegrityError):
        async with db_session() as db:
            db.add(ErrorSignature(fingerprint="dup"))
            await db.commit()


def test_migration_0034_chains_off_the_current_head():
    mod = importlib.import_module("server.db.migrations.versions.0034_error_signatures")
    assert mod.revision == "0034_error_signatures"
    assert mod.down_revision == "0033_triage_items"


def test_error_sink_flag_default_and_env(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("CCSWITCH_ERROR_SINK_ENABLED", raising=False)
    assert Settings().error_sink_enabled is True
    monkeypatch.setenv("CCSWITCH_ERROR_SINK_ENABLED", "false")
    assert Settings().error_sink_enabled is False


def test_error_sink_flag_listed_on_about():
    from server.api.settings import _MANAGED
    assert "error_sink_enabled" in {row[0] for row in _MANAGED}


def test_dropped_counter_exists():
    from server.obs import metrics
    before = metrics.error_sink_dropped_total.value()
    metrics.error_sink_dropped_total.inc()
    assert metrics.error_sink_dropped_total.value() == before + 1
```

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_error_signature_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'naive_utc_now'` and friends.

- [ ] **Step 3: Add `naive_utc_now` to `server/db/clock.py`**

Append at the end of the file:

```python
def naive_utc_now() -> "datetime":
    """Python-side "now" in the schema's convention: naive UTC.

    The writer-side twin of :func:`db_utc_now`. Use it for any timestamp you
    compute in Python and compare against or store in a schema column, instead
    of re-spelling ``datetime.now(timezone.utc).replace(tzinfo=None)`` at each
    call site.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)
```

Also add `from datetime import datetime` under `TYPE_CHECKING` at the top if mypy complains about the string annotation; simplest is a real import at the top: `from datetime import datetime, timezone` and drop the inner import.

- [ ] **Step 4: Add the model after `SessionFeedback` in `server/db/models.py`**

```python
class ErrorSignature(Base):
    """One kind of WARNING+ log record, deduplicated by fingerprint (error-triage
    spec §5). The sink (server/obs/errsink.py) upserts counts and a recent sample;
    the error-triage automation files issues and drives the status lattice
    observed → filed → resolved, muted from any state.

    Every column is nullable so `_reconcile_columns` adds the table on both
    dialects; every timestamp is naive UTC (server/db/clock.py)."""

    __tablename__ = "error_signatures"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)      # edge | daemon | client
    level: Mapped[str | None] = mapped_column(String, nullable=True)       # WARNING | ERROR | CRITICAL
    logger: Mapped[str | None] = mapped_column(String, nullable=True)
    event: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    sample_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_exc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # request_id, proc_epoch, host, app_version, url of the sample + recent_sessions (≤5 ids)
    sample_context: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    sample_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    # Occurrences since the last file or comment; the sink adds, the triage tick subtracts.
    count_since_filed: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    # observed | filed | resolved | muted
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="observed", index=True)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    muted_by: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    filed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    last_commented_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    issue_closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    muted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
```

(`models.py` cannot import `server.db.clock` without a cycle risk — `clock.py` imports only SQLAlchemy, so `from server.db.clock import naive_utc_now` IS safe; use `default=naive_utc_now` if the import works cleanly, otherwise keep the lambda as `SessionFeedback` does.)

- [ ] **Step 5: Write the migration**

```python
# server/db/migrations/versions/0034_error_signatures.py
"""Error triage: error_signatures — deduplicated WARNING+ log signatures.

Revision ID: 0034_error_signatures
Revises: 0033_triage_items
Create Date: 2026-09-07

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
create_all builds the new table on both SQLite and Postgres at next boot, and
_reconcile_columns ALTER-ADDs any later nullable column.
"""
from alembic import op
import sqlalchemy as sa

revision = "0034_error_signatures"
down_revision = "0033_triage_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "error_signatures",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=True, unique=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("level", sa.String(), nullable=True),
        sa.Column("logger", sa.String(), nullable=True),
        sa.Column("event", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("sample_message", sa.Text(), nullable=True),
        sa.Column("sample_exc", sa.Text(), nullable=True),
        sa.Column("sample_context", sa.JSON(), nullable=True),
        sa.Column("sample_at", sa.DateTime(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=True),
        sa.Column("count_since_filed", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("muted_by", sa.String(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("filed_at", sa.DateTime(), nullable=True),
        sa.Column("last_commented_at", sa.DateTime(), nullable=True),
        sa.Column("issue_closed_at", sa.DateTime(), nullable=True),
        sa.Column("muted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_error_signatures_fingerprint", "error_signatures", ["fingerprint"])
    op.create_index("ix_error_signatures_status", "error_signatures", ["status"])


def downgrade() -> None:
    op.drop_index("ix_error_signatures_status", table_name="error_signatures")
    op.drop_index("ix_error_signatures_fingerprint", table_name="error_signatures")
    op.drop_table("error_signatures")
```

- [ ] **Step 6: Add the settings flag in both `Settings` definitions**

Pydantic block (after `session_feedback_cap`, ~line 269):

```python
        # Error-triage capture (spec 2026-09-07): install the WARNING+ log sink that
        # feeds error_signatures. Off = configure_logging() installs no handler.
        error_sink_enabled: bool = Field(
            default=True, alias="CCSWITCH_ERROR_SINK_ENABLED")
```

Dataclass fallback (after `session_feedback_cap`, ~line 563):

```python
        error_sink_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ERROR_SINK_ENABLED", "true").lower() in ("1", "true", "yes", "on")
        )
```

`_MANAGED` in `server/api/settings.py`, after the `session_feedback_cap` row:

```python
    ("error_sink_enabled", "CCSWITCH_ERROR_SINK_ENABLED", "Capture WARNING+ log records into error signatures for Error Triage"),
```

- [ ] **Step 7: Add the metric** (after `session_feedback_total` in `server/obs/metrics.py`):

```python
error_sink_dropped_total = Counter(
    "error_sink_dropped_total",
    "Log records the error sink dropped because its per-flush fingerprint map was full")
```

- [ ] **Step 8: Run the tests plus the schema guards**

Run: `$PY -m pytest server/tests/test_error_signature_model.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -v`
Expected: PASS (Postgres tests skip without `CCSWITCH_TEST_PG_URL`; that is fine locally — CI runs them, and no `_REQUIRED_EXTRAS` entry is needed because every column is nullable).

- [ ] **Step 9: Commit**

```bash
git add server/db/clock.py server/db/models.py server/db/migrations/versions/0034_error_signatures.py server/config.py server/api/settings.py server/obs/metrics.py server/tests/test_error_signature_model.py
printf 'feat(db): error_signatures table, CCSWITCH_ERROR_SINK_ENABLED flag, naive_utc_now\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git commit -F /tmp/c3.txt
```

---
### Task 4: `ErrorSink` handler, flusher and DB upsert — `server/obs/errsink.py`

**Files:**
- Create: `server/obs/errsink.py`
- Modify: `server/obs/jsonlog.py` (end of `configure_logging`)
- Test: `server/tests/test_errsink.py`

**Interfaces:**
- Consumes: `signature_of`, `Signature` (Task 2); `ErrorSignature`, `naive_utc_now`, `error_sink_dropped_total`, `Settings.error_sink_enabled` (Task 3); `jsonlog.PROCESS` (Task 1).
- Produces:
  - `class ErrorSink(logging.Handler)` with `__init__(self, *, source: str, host: str)`, `emit(record)`, `drain() -> dict[str, _Pending]`, `start() -> None`, `async stop() -> None`, `async flush_once() -> int`, `is_running -> bool`, `dropped: int`.
  - `install_error_sink(*, source: str) -> ErrorSink` (idempotent; returns the existing one), `get_error_sink() -> ErrorSink | None`, `reset_for_tests() -> None`.
  - Module constants `MAX_PENDING = 512`, `FLUSH_INTERVAL_S = 5.0`, `SAMPLE_REFRESH_S = 86400`, `RECENT_SESSIONS = 5`.
  - `_Pending` dataclass: `sig: Signature`, `count: int`, `first_seen: datetime`, `last_seen: datetime`, `sessions: list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_errsink.py
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest


def _rec(msg="boom", *, level=logging.ERROR, name="server.x", **extra):
    rec = logging.LogRecord(name, level, "/repo/server/x.py", 10, msg, (), None, func="f")
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def _sink():
    from server.obs.errsink import ErrorSink
    return ErrorSink(source="edge", host="dobby")


def test_emit_aggregates_same_fingerprint():
    sink = _sink()
    for i in range(10):
        sink.emit(_rec(f"db locked after {i} retries", session_id=f"s{i % 2}"))
    pend = sink.drain()
    assert len(pend) == 1
    p = next(iter(pend.values()))
    assert p.count == 10
    assert p.sessions == ["s0", "s1"]
    assert sink.drain() == {}  # drain empties


def test_emit_ignores_info_and_own_logger():
    sink = _sink()
    sink.emit(_rec("info", level=logging.INFO))
    sink.emit(_rec("x", name="server.obs.errsink"))
    assert sink.drain() == {}


def test_emit_is_bounded_and_counts_drops(monkeypatch):
    from server.obs import errsink, metrics
    monkeypatch.setattr(errsink, "MAX_PENDING", 2)
    before = metrics.error_sink_dropped_total.value()
    sink = _sink()
    for msg in ("alpha", "beta", "gamma"):
        sink.emit(_rec(msg))
    assert len(sink.drain()) == 2
    assert sink.dropped == 1
    assert metrics.error_sink_dropped_total.value() == before + 1


def test_client_error_counted_once_despite_bare_reemit():
    sink = _sink()
    sink.emit(_rec("client.error", name="client", event="client.error",
                   fields={"msg": "TypeError: x", "stack": ""}))
    sink.emit(_rec("TypeError: x", name="client", source="frontend"))
    pend = sink.drain()
    assert len(pend) == 1
    p = next(iter(pend.values()))
    assert p.count == 1 and p.sig.source == "client"


def test_emit_never_raises(monkeypatch):
    from server.obs import errsink
    def boom(*a, **k):
        raise RuntimeError("signature exploded")
    monkeypatch.setattr(errsink, "signature_of", boom)
    sink = _sink()
    sink.emit(_rec("x"))  # must not raise
    assert sink.drain() == {}


def test_emit_is_reentrancy_safe(monkeypatch):
    """A record logged while the sink is inside emit() is ignored, not recursed."""
    from server.obs import errsink
    sink = _sink()
    real = errsink.signature_of
    def nested(record, **kw):
        sink.emit(_rec("nested"))  # re-enter
        return real(record, **kw)
    monkeypatch.setattr(errsink, "signature_of", nested)
    sink.emit(_rec("outer"))
    pend = sink.drain()
    assert [p.sig.sample_message for p in pend.values()] == ["outer"]


@pytest.mark.asyncio
async def test_flush_inserts_then_increments(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import ErrorSignature
    sink = _sink()
    for _ in range(3):
        sink.emit(_rec("boom", session_id="s1"))
    assert await sink.flush_once() == 1
    for _ in range(2):
        sink.emit(_rec("boom", session_id="s2"))
    await sink.flush_once()
    async with db_session() as db:
        row = (await db.execute(select(ErrorSignature))).scalars().one()
    assert row.count == 5 and row.count_since_filed == 5
    assert row.status == "observed" and row.source == "edge"
    assert row.sample_context["recent_sessions"] == ["s2", "s1"]
    assert row.sample_context["host"] == "dobby"
    assert row.first_seen_at <= row.last_seen_at


@pytest.mark.asyncio
async def test_flush_refreshes_sample_only_when_stale(isolated_db):
    from sqlalchemy import select, update
    from server.db import db_session
    from server.db.models import ErrorSignature
    sink = _sink()
    sink.emit(_rec("db locked after 1 retries"))
    await sink.flush_once()
    sink.emit(_rec("db locked after 2 retries"))  # same fingerprint, newer text
    await sink.flush_once()
    async with db_session() as db:
        row = (await db.execute(select(ErrorSignature))).scalars().one()
        assert row.sample_message == "db locked after 1 retries"  # fresh → kept
        await db.execute(update(ErrorSignature).values(
            sample_at=row.sample_at - timedelta(hours=25)))
    sink.emit(_rec("db locked after 3 retries"))
    await sink.flush_once()
    async with db_session() as db:
        row = (await db.execute(select(ErrorSignature))).scalars().one()
    assert row.sample_message == "db locked after 3 retries"  # stale → refreshed


@pytest.mark.asyncio
async def test_flush_failure_requeues_bounded(monkeypatch):
    from server.obs import errsink
    async def fail(batch):
        raise RuntimeError("db down")
    monkeypatch.setattr(errsink, "_upsert", fail)
    sink = _sink()
    sink.emit(_rec("boom"))
    await sink.flush_once()
    pend = sink.drain()
    assert len(pend) == 1 and next(iter(pend.values())).count == 1


@pytest.mark.asyncio
async def test_start_and_stop_drain(isolated_db, monkeypatch):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import ErrorSignature
    from server.obs import errsink
    monkeypatch.setattr(errsink, "FLUSH_INTERVAL_S", 0.05)
    sink = _sink()
    sink.start()
    assert sink.is_running
    sink.start()  # idempotent
    sink.emit(_rec("boom"))
    await asyncio.sleep(0.2)
    async with db_session() as db:
        assert (await db.execute(select(ErrorSignature))).scalars().one().count == 1
    sink.emit(_rec("boom"))
    await sink.stop()  # cancels the loop, then flushes what is pending
    assert not sink.is_running
    async with db_session() as db:
        assert (await db.execute(select(ErrorSignature))).scalars().one().count == 2


def test_configure_logging_installs_sink_once(monkeypatch):
    from server.obs import errsink, jsonlog
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(jsonlog, "_configured", False)
    monkeypatch.delenv("CCSWITCH_ERROR_SINK_ENABLED", raising=False)
    errsink.reset_for_tests()
    try:
        jsonlog.configure_logging(process="daemon")
        sink = errsink.get_error_sink()
        assert sink is not None and sink in root.handlers
        assert sink._source == "daemon"
        assert errsink.install_error_sink(source="edge") is sink  # idempotent, keeps the first
    finally:
        errsink.reset_for_tests()
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_configure_logging_respects_kill_switch(monkeypatch):
    from server.obs import errsink, jsonlog
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(jsonlog, "_configured", False)
    monkeypatch.setenv("CCSWITCH_ERROR_SINK_ENABLED", "false")
    errsink.reset_for_tests()
    try:
        jsonlog.configure_logging()
        assert errsink.get_error_sink() is None
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
```

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_errsink.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.obs.errsink'`.

- [ ] **Step 3: Implement the sink**

```python
# server/obs/errsink.py
"""ErrorSink — in-process capture of WARNING+ log records (error-triage spec §4).

A logging.Handler on the root logger folds records into an in-memory map keyed
by fingerprint (server/obs/error_signature.py); a small asyncio task flushes the
map to ``error_signatures`` every FLUSH_INTERVAL_S. A 10 000-record error loop
becomes one upsert with count 10 000.

Guarantees: emit() never blocks on I/O, never raises into the logging call site,
ignores records from this module's own logger, is a no-op when re-entered on
the same thread, and holds at most MAX_PENDING fingerprints between flushes
(overflow is counted in error_sink_dropped_total). A failed flush merges the
batch back — bounded — and retries next interval.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from server.obs import metrics
from server.obs.error_signature import OWN_LOGGER, Signature, signature_of

log = logging.getLogger(OWN_LOGGER)
assert log.name == __name__, "OWN_LOGGER must equal this module's logger name"

MAX_PENDING = 512
FLUSH_INTERVAL_S = 5.0
SAMPLE_REFRESH_S = 24 * 3600
RECENT_SESSIONS = 5


def _now() -> datetime:
    from server.db.clock import naive_utc_now
    return naive_utc_now()


@dataclass
class _Pending:
    sig: Signature
    first_seen: datetime
    last_seen: datetime
    count: int = 1
    sessions: list[str] = field(default_factory=list)


class ErrorSink(logging.Handler):
    def __init__(self, *, source: str, host: str) -> None:
        super().__init__(level=logging.WARNING)
        self._source = source
        self._host = host
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._guard = threading.local()
        self._task: asyncio.Task | None = None
        self.dropped = 0

    # -- capture (any thread) ---------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._guard, "active", False) or record.name == OWN_LOGGER:
            return
        self._guard.active = True
        try:
            sig = signature_of(record, source=self._source, host=self._host)
            if sig is None:
                return
            now = _now()
            with self._lock:
                p = self._pending.get(sig.fingerprint)
                if p is None:
                    if len(self._pending) >= MAX_PENDING:
                        self.dropped += 1
                        metrics.error_sink_dropped_total.inc()
                        return
                    p = _Pending(sig=sig, first_seen=now, last_seen=now)
                    self._pending[sig.fingerprint] = p
                else:
                    p.count += 1
                    p.last_seen = now
                if sig.session_id and sig.session_id not in p.sessions:
                    p.sessions.append(sig.session_id)
                    del p.sessions[RECENT_SESSIONS:]
        except Exception:  # noqa: BLE001 — a handler must never raise into the caller
            pass
        finally:
            self._guard.active = False

    def drain(self) -> dict[str, _Pending]:
        with self._lock:
            out, self._pending = self._pending, {}
        return out

    # -- flusher (event loop) ----------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the periodic flusher on the running loop. Idempotent."""
        if self.is_running:
            return
        self._task = asyncio.create_task(self._loop(), name="error-sink-flush")

    async def stop(self) -> None:
        from server.aio import cancel_and_reap
        await cancel_and_reap(self._task, phase="error-sink-flush")
        self._task = None
        await self.flush_once()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            await self.flush_once()

    async def flush_once(self) -> int:
        """Upsert everything pending. Returns the number of fingerprints flushed."""
        batch = self.drain()
        if not batch:
            return 0
        try:
            await _upsert(batch)
        except Exception:  # noqa: BLE001 — DB trouble must not kill the loop
            log.debug("errsink.flush_failed n=%d", len(batch), exc_info=True)
            with self._lock:
                for fp, p in batch.items():
                    cur = self._pending.get(fp)
                    if cur is not None:
                        cur.count += p.count
                        cur.first_seen = min(cur.first_seen, p.first_seen)
                    elif len(self._pending) < MAX_PENDING:
                        self._pending[fp] = p
                    else:
                        self.dropped += 1
                        metrics.error_sink_dropped_total.inc()
            return 0
        return len(batch)


async def _upsert(batch: dict[str, _Pending]) -> None:
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import ErrorSignature

    async with db_session() as db:
        existing = {
            r.fingerprint: r for r in (await db.execute(
                select(ErrorSignature).where(ErrorSignature.fingerprint.in_(list(batch)))
            )).scalars()
        }
        for fp, p in batch.items():
            row = existing.get(fp)
            if row is None:
                db.add(ErrorSignature(
                    fingerprint=fp, source=p.sig.source, level=p.sig.level,
                    logger=p.sig.logger, event=p.sig.event, title=p.sig.title,
                    sample_message=p.sig.sample_message, sample_exc=p.sig.sample_exc,
                    sample_context={**p.sig.context, "recent_sessions": list(p.sessions)},
                    sample_at=p.last_seen, count=p.count, count_since_filed=p.count,
                    status="observed", first_seen_at=p.first_seen, last_seen_at=p.last_seen,
                ))
                continue
            row.count = (row.count or 0) + p.count
            row.count_since_filed = (row.count_since_filed or 0) + p.count
            row.last_seen_at = p.last_seen
            ctx = dict(row.sample_context or {})
            recent = list(p.sessions) + [s for s in ctx.get("recent_sessions", [])
                                         if s not in p.sessions]
            ctx["recent_sessions"] = recent[:RECENT_SESSIONS]
            stale = row.sample_at is None or \
                (p.last_seen - row.sample_at).total_seconds() >= SAMPLE_REFRESH_S
            if stale:
                row.sample_message = p.sig.sample_message
                row.sample_exc = p.sig.sample_exc
                row.sample_at = p.last_seen
                ctx.update({k: v for k, v in p.sig.context.items() if v is not None})
            row.sample_context = ctx  # reassign: JSON columns don't track in-place edits


# -- process-wide singleton -------------------------------------------------------
_sink: ErrorSink | None = None


def install_error_sink(*, source: str) -> ErrorSink:
    """Create the process's sink and attach it to the root logger. Idempotent."""
    global _sink
    if _sink is not None:
        return _sink
    try:
        from server.hostname import self_host_name
        host = self_host_name()
    except Exception:  # noqa: BLE001 — a hostname lookup must not block logging setup
        host = "unknown"
    _sink = ErrorSink(source=source, host=host)
    logging.getLogger().addHandler(_sink)
    return _sink


def get_error_sink() -> ErrorSink | None:
    return _sink


def reset_for_tests() -> None:
    global _sink
    if _sink is not None:
        logging.getLogger().removeHandler(_sink)
    _sink = None
```

- [ ] **Step 4: Install it from `configure_logging`**

In `server/obs/jsonlog.py`, just before `_configured = True` at the end of `configure_logging`:

```python
    # Error-triage capture (spec 2026-09-07 §4): WARNING+ records → error_signatures.
    # Installed here so the edge and the daemon get it from the same call; the
    # flusher is started by each process's lifespan once the DB is up.
    if bool(getattr(settings, "error_sink_enabled", True)):
        from server.obs.errsink import install_error_sink
        install_error_sink(source=process)
```

- [ ] **Step 5: Run the tests**

Run: `$PY -m pytest server/tests/test_errsink.py server/tests/test_error_signature.py server/tests/test_daemon_configures_logging.py server/tests/test_no_swallowed_outer_cancellation.py -v`
Expected: PASS. The daemon test's `finally` already removes any handler added during the call, so the sink installed there is cleaned up; if it leaks, add `errsink.reset_for_tests()` to that test's `finally`.

- [ ] **Step 6: Commit**

```bash
git add server/obs/errsink.py server/obs/jsonlog.py server/tests/test_errsink.py server/tests/test_daemon_configures_logging.py
printf 'feat(obs): ErrorSink — bounded WARNING+ capture with periodic upsert\n\nInstalled by configure_logging() behind CCSWITCH_ERROR_SINK_ENABLED.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git commit -F /tmp/c4.txt
```

---

### Task 5: Start and stop the flusher in both lifespans

**Files:**
- Modify: `server/app.py` (after `await init_db(db_url=db_url)` at ~line 211; shutdown loop at ~line 487)
- Modify: `server/ipc/serve.py` (`daemon_lifespan`, after `await init_db(db_url=db_url)` at ~line 115; shutdown, next to the `loop_lag_task` reap at ~line 278)
- Test: `server/tests/test_errsink.py` (append one test)

**Interfaces:**
- Consumes: `get_error_sink()`, `ErrorSink.start()/stop()` (Task 4).

- [ ] **Step 1: Write the failing test** (append to `server/tests/test_errsink.py`)

```python
def test_edge_lifespan_runs_the_flusher(monkeypatch, tmp_path):
    """The edge lifespan starts the sink's flusher after init_db and stops it on shutdown."""
    from fastapi.testclient import TestClient
    from server.obs import errsink, jsonlog
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    monkeypatch.delenv("CCSWITCH_TAILNET_ALLOWLIST", raising=False)
    monkeypatch.setenv("CCSWITCH_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}")
    monkeypatch.setattr(jsonlog, "_configured", False)
    root = logging.getLogger()
    before = list(root.handlers)
    errsink.reset_for_tests()
    try:
        from server.app import app
        with TestClient(app):
            sink = errsink.get_error_sink()
            assert sink is not None and sink.is_running
        assert not sink.is_running
    finally:
        errsink.reset_for_tests()
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
```

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_errsink.py::test_edge_lifespan_runs_the_flusher -v`
Expected: FAIL — `assert sink.is_running` is False.

- [ ] **Step 3: Wire the edge**

In `server/app.py:_lifespan`, immediately after `await init_db(db_url=db_url)`:

```python
    # Error-triage capture: the sink itself was installed by configure_logging();
    # its flusher needs the DB, so it starts here and stops in the shutdown branch.
    from server.obs.errsink import get_error_sink as _get_error_sink
    _error_sink = _get_error_sink()
    if _error_sink is not None:
        _error_sink.start()
```

In the shutdown branch, right after the `for _attr, _phase in (("loop_lag_task", …)` loop finishes (before `app.state.auth_failure_auditor.stop()`):

```python
    if _error_sink is not None:
        await bounded_wait(_error_sink.stop(), timeout=_SHUTDOWN_PHASE_TIMEOUT_S,
                           phase="shutdown:error-sink")
```

- [ ] **Step 4: Wire the daemon**

In `server/ipc/serve.py:daemon_lifespan`, immediately after `await init_db(db_url=db_url)`:

```python
    from server.obs.errsink import get_error_sink
    error_sink = get_error_sink()
    if error_sink is not None:
        error_sink.start()
```

In the shutdown part, right after the `loop_lag_task` reap (`await cancel_and_reap(loop_lag_task, phase="daemon.loop-lag")`):

```python
        if error_sink is not None:
            try:
                await asyncio.wait_for(error_sink.stop(), timeout=_DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — never let the sink block daemon shutdown
                log.warning("daemon shutdown: error sink stop failed", exc_info=True)
```

(`asyncio.wait_for` raising `TimeoutError` is an `Exception`; a caller cancellation propagates as `CancelledError`, which is a `BaseException` and is NOT swallowed — this is the shape `test_no_swallowed_outer_cancellation.py` allows.)

- [ ] **Step 5: Run the tests**

Run: `$PY -m pytest server/tests/test_errsink.py server/tests/test_ipc_entrypoint.py server/tests/test_no_swallowed_outer_cancellation.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/app.py server/ipc/serve.py server/tests/test_errsink.py
printf 'feat(obs): start/stop the error-sink flusher in the edge and daemon lifespans\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt
git commit -F /tmp/c5.txt
```

---
### Task 6: `comment_on_issue` and `issue_states` in `server/github/ci_issues.py`

**Files:**
- Modify: `server/github/ci_issues.py` (append after `comment_on_pr`)
- Test: `server/tests/test_ci_issues_comment_states.py`

**Interfaces:**
- Produces:
  - `comment_on_issue(repo: str, number: int, body: str, *, run=gitrepos._run_gh) -> str | None` — comment html_url, `None` on failure, never raises.
  - `issue_states(repo: str, numbers: Sequence[int], *, run=gitrepos._run_gh) -> dict[int, tuple[bool, str | None]]` — `{number: (closed, state_reason)}` with `state_reason` lowercased (`"completed"`, `"not_planned"`, `"reopened"`, or `None`). One GraphQL call for up to 100 numbers; `{}` on failure or empty input.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_ci_issues_comment_states.py
from __future__ import annotations

import json
import subprocess


def _runner(calls, *, rc=0, stdout=""):
    def run(args, *a, **k):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, rc, stdout, "nope" if rc else "")
    return run


def test_comment_on_issue_posts_and_returns_url():
    from server.github.ci_issues import comment_on_issue
    calls: list[list[str]] = []
    url = comment_on_issue("o/r", 7, "hello",
                           run=_runner(calls, stdout=json.dumps({"html_url": "https://x/c/1"})))
    assert url == "https://x/c/1"
    assert calls[0][:2] == ["api", "repos/o/r/issues/7/comments"]
    assert "body=hello" in calls[0]


def test_comment_on_issue_failure_returns_none():
    from server.github.ci_issues import comment_on_issue
    assert comment_on_issue("o/r", 7, "x", run=_runner([], rc=1)) is None
    def raiser(*a, **k):
        raise OSError("gh missing")
    assert comment_on_issue("o/r", 7, "x", run=raiser) is None


def test_issue_states_one_graphql_call():
    from server.github.ci_issues import issue_states
    calls: list[list[str]] = []
    payload = {"data": {"repository": {
        "i1": {"number": 1, "closed": True, "stateReason": "NOT_PLANNED"},
        "i2": {"number": 2, "closed": False, "stateReason": None},
        "i3": None,
        "i4": {"number": 4, "closed": True, "stateReason": "COMPLETED"},
    }}}
    out = issue_states("o/r", [1, 2, 3, 4], run=_runner(calls, stdout=json.dumps(payload)))
    assert out == {1: (True, "not_planned"), 2: (False, None), 4: (True, "completed")}
    assert len(calls) == 1 and calls[0][:2] == ["api", "graphql"]
    query = calls[0][3]
    assert 'repository(owner: "o", name: "r")' in query
    assert "i4: issue(number: 4)" in query


def test_issue_states_empty_and_failure():
    from server.github.ci_issues import issue_states
    calls: list[list[str]] = []
    assert issue_states("o/r", [], run=_runner(calls)) == {}
    assert calls == []
    assert issue_states("o/r", [1], run=_runner(calls, rc=1)) == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_ci_issues_comment_states.py -v`
Expected: FAIL — `ImportError: cannot import name 'comment_on_issue'`.

- [ ] **Step 3: Implement** (append after `comment_on_pr` in `server/github/ci_issues.py`)

```python
def comment_on_issue(repo: str, number: int, body: str, *,
                     run=gitrepos._run_gh) -> str | None:
    """Comment on an issue, returning the comment's html_url; None on failure.

    Same endpoint as ``comment_on_pr`` (GitHub shares one comment API across
    issues and PRs) — kept as its own name so call sites in the filers read as
    what they are. Used by the error-triage recurrence path and, later, the
    session-feedback filer (#1434) and the CI/CD Doctor (#1433).
    """
    try:
        proc = run(["api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"])
        if proc.returncode != 0:
            log.warning("github.issue_comment_failed repo=%s #%s: %s",
                        repo, number, proc.stderr.strip())
            return None
        return str(json.loads(proc.stdout)["html_url"])
    except Exception:
        log.warning("github.issue_comment_failed repo=%s #%s", repo, number, exc_info=True)
        return None


def issue_states(repo: str, numbers: Sequence[int], *,
                 run=gitrepos._run_gh) -> dict[int, tuple[bool, str | None]]:
    """``{number: (closed, state_reason)}`` for up to 100 issues in ONE GraphQL call.

    ``state_reason`` is GitHub's ``stateReason`` lowercased ("completed",
    "not_planned", "reopened") or None. Issues that do not exist are omitted.
    Returns {} on any failure — callers treat "unknown" as "unchanged".
    """
    wanted = [int(n) for n in numbers][:100]
    if not wanted:
        return {}
    owner, name = repo.split("/", 1)
    aliases = " ".join(f"i{n}: issue(number: {n}) {{ number closed stateReason }}"
                       for n in wanted)
    query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {aliases} }} }}'
    try:
        proc = run(["api", "graphql", "-f", f"query={query}"])
        if proc.returncode != 0:
            log.warning("github.issue_states_failed repo=%s: %s", repo, proc.stderr.strip())
            return {}
        repo_data = (json.loads(proc.stdout).get("data") or {}).get("repository") or {}
        out: dict[int, tuple[bool, str | None]] = {}
        for value in repo_data.values():
            if not value:
                continue
            reason = value.get("stateReason")
            out[int(value["number"])] = (bool(value.get("closed")),
                                         str(reason).lower() if reason else None)
        return out
    except Exception:
        log.warning("github.issue_states_failed repo=%s", repo, exc_info=True)
        return {}
```

`valid_repo(repo)` is the caller's job (the triage loop checks it before any call), matching how the module's other functions trust their `repo`.

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest server/tests/test_ci_issues_comment_states.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/github/ci_issues.py server/tests/test_ci_issues_comment_states.py
printf 'feat(github): comment_on_issue and batched issue_states\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c6.txt
git commit -F /tmp/c6.txt
```

---

### Task 7: The shared filing decision — `server/automations/issue_filing.py`

**Files:**
- Create: `server/automations/issue_filing.py`
- Test: `server/tests/test_issue_filing.py`

**Interfaces:**
- Consumes: `ci_issues.create_issue`, `ci_issues.comment_on_issue` (Task 6).
- Produces:
  - `@dataclass(frozen=True) Linked(number: int | None, closed: bool = False, closed_reason: str | None = None, last_commented_at: datetime | None = None)`
  - `@dataclass(frozen=True) Decision(kind: str, reason: str)` — `kind ∈ {"file","comment","regress","skip"}`, `reason ∈ {"no_issue","open","closed","comment_throttled","daily_cap","muted_by_close","retry"}`
  - `@dataclass(frozen=True) Applied(issue_number: int | None, commented: bool)`
  - `decide(linked: Linked | None, *, cap_remaining: int, comment_every: timedelta, now: datetime) -> Decision`
  - `async apply(decision: Decision, *, repo: str, title: str, body: str, labels: Sequence[str], linked: Linked | None, run=gitrepos._run_gh) -> Applied` — never raises.
  - `neutralize_markers(text: str) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_issue_filing.py
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta

import pytest

NOW = datetime(2026, 9, 7, 12, 0, 0)
DAY = timedelta(hours=24)


def _decide(linked, cap=5):
    from server.automations.issue_filing import decide
    return decide(linked, cap_remaining=cap, comment_every=DAY, now=NOW)


def test_decide_no_issue_files_or_hits_cap():
    from server.automations.issue_filing import Linked
    assert (_decide(None).kind, _decide(None).reason) == ("file", "no_issue")
    assert _decide(Linked(number=None)).kind == "file"
    d = _decide(None, cap=0)
    assert (d.kind, d.reason) == ("skip", "daily_cap")


def test_decide_open_issue_comments_then_throttles():
    from server.automations.issue_filing import Linked
    fresh = _decide(Linked(number=7, closed=False, last_commented_at=None))
    assert (fresh.kind, fresh.reason) == ("comment", "open")
    old = _decide(Linked(number=7, last_commented_at=NOW - DAY))
    assert old.kind == "comment"
    recent = _decide(Linked(number=7, last_commented_at=NOW - timedelta(hours=1)))
    assert (recent.kind, recent.reason) == ("skip", "comment_throttled")
    # an open issue never files a second one, cap or not
    assert _decide(Linked(number=7), cap=0).kind == "skip"


def test_decide_closed_completed_regresses_and_not_planned_mutes():
    from server.automations.issue_filing import Linked
    d = _decide(Linked(number=7, closed=True, closed_reason="completed"))
    assert (d.kind, d.reason) == ("regress", "closed")
    capped = _decide(Linked(number=7, closed=True, closed_reason="completed"), cap=0)
    assert (capped.kind, capped.reason) == ("skip", "daily_cap")
    muted = _decide(Linked(number=7, closed=True, closed_reason="not_planned"))
    assert (muted.kind, muted.reason) == ("skip", "muted_by_close")
    # closed with no reason (older issues) behaves like completed
    assert _decide(Linked(number=7, closed=True)).kind == "regress"


def _gh(calls, *, fail_create=False, number=42):
    def run(args, *a, **k):
        calls.append(list(args))
        path = args[1] if len(args) > 1 else ""
        if path.endswith("/labels"):
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if path.endswith("/comments"):
            return subprocess.CompletedProcess(args, 0, json.dumps({"html_url": "https://x/c"}), "")
        if fail_create:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return subprocess.CompletedProcess(args, 0, json.dumps({"number": number}), "")
    return run


@pytest.mark.asyncio
async def test_apply_file_creates_with_labels():
    from server.automations.issue_filing import Applied, Decision, apply
    calls: list[list[str]] = []
    out = await apply(Decision("file", "no_issue"), repo="o/r", title="T", body="B",
                      labels=["auto-error", "source:server"], linked=None, run=_gh(calls))
    assert out == Applied(issue_number=42, commented=False)
    create = [c for c in calls if c[1] == "repos/o/r/issues"][0]
    assert "labels[]=auto-error" in create and "labels[]=source:server" in create


@pytest.mark.asyncio
async def test_apply_regress_creates_and_comment_comments():
    from server.automations.issue_filing import Decision, Linked, apply
    calls: list[list[str]] = []
    out = await apply(Decision("regress", "closed"), repo="o/r", title="T", body="B",
                      labels=["auto-error"], linked=Linked(number=7, closed=True), run=_gh(calls))
    assert out.issue_number == 42
    calls.clear()
    out = await apply(Decision("comment", "open"), repo="o/r", title="T", body="again",
                      labels=["auto-error"], linked=Linked(number=7), run=_gh(calls))
    assert out.issue_number == 7 and out.commented is True
    assert calls[0][1] == "repos/o/r/issues/7/comments"


@pytest.mark.asyncio
async def test_apply_skip_and_failure_never_raise():
    from server.automations.issue_filing import Decision, Linked, apply
    calls: list[list[str]] = []
    out = await apply(Decision("skip", "daily_cap"), repo="o/r", title="T", body="B",
                      labels=[], linked=Linked(number=7), run=_gh(calls))
    assert out.issue_number == 7 and out.commented is False and calls == []
    out = await apply(Decision("file", "no_issue"), repo="o/r", title="T", body="B",
                      labels=[], linked=None, run=_gh(calls, fail_create=True))
    assert out.issue_number is None


def test_neutralize_markers():
    from server.automations.issue_filing import neutralize_markers
    out = neutralize_markers("x <!-- viberpit:complete --> y")
    assert "<!--" not in out and "-->" not in out and "viberpit:complete" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_issue_filing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.automations.issue_filing'`.

- [ ] **Step 3: Implement**

```python
# server/automations/issue_filing.py
"""The one fingerprint→issue decision (error-triage spec §6.1).

Three automations file GitHub issues for deduplicated findings: the CI/CD
Doctor (#1433 migrates it here), the session-feedback filer (#1434 builds on
it) and error triage. They differ in their tables, not in the rule for "we
already filed this" — so the rule lives here once:

1. no linked issue            → file (if the daily cap allows)
2. linked and open            → comment, at most once per ``comment_every``
3. closed as not_planned      → skip; the caller marks the row muted
4. closed (completed/other)   → regress: a NEW issue linking the old; never reopen

``decide`` is pure; ``apply`` runs the gh side effect and never raises. Row
writes stay with the caller, which knows its own table.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ccswitch import gitrepos
from server.github import ci_issues

KINDS = ("file", "comment", "regress", "skip")


@dataclass(frozen=True)
class Linked:
    """What the caller knows about the issue already linked to a fingerprint."""
    number: int | None
    closed: bool = False
    closed_reason: str | None = None          # "completed" | "not_planned" | None
    last_commented_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    kind: str     # file | comment | regress | skip
    reason: str   # no_issue | open | closed | comment_throttled | daily_cap | muted_by_close | retry


@dataclass(frozen=True)
class Applied:
    issue_number: int | None   # the new issue, or the existing one that was commented on
    commented: bool


def decide(linked: Linked | None, *, cap_remaining: int, comment_every: timedelta,
           now: datetime) -> Decision:
    if linked is None or linked.number is None:
        return Decision("file", "no_issue") if cap_remaining > 0 else Decision("skip", "daily_cap")
    if not linked.closed:
        if linked.last_commented_at is None or now - linked.last_commented_at >= comment_every:
            return Decision("comment", "open")
        return Decision("skip", "comment_throttled")
    if linked.closed_reason == "not_planned":
        return Decision("skip", "muted_by_close")
    return Decision("regress", "closed") if cap_remaining > 0 else Decision("skip", "daily_cap")


async def apply(decision: Decision, *, repo: str, title: str, body: str,
                labels: Sequence[str], linked: Linked | None,
                run=gitrepos._run_gh) -> Applied:
    """Run the gh side effect for *decision*. Never raises; a failed create is
    ``Applied(None, False)`` so the caller can leave its row retryable."""
    existing = linked.number if linked is not None else None
    if decision.kind in ("file", "regress"):
        number = await asyncio.to_thread(
            ci_issues.create_issue, repo, title, body, labels=list(labels), run=run)
        return Applied(issue_number=number, commented=False)
    if decision.kind == "comment" and existing is not None:
        url = await asyncio.to_thread(ci_issues.comment_on_issue, repo, existing, body, run=run)
        return Applied(issue_number=existing, commented=url is not None)
    return Applied(issue_number=existing, commented=False)


def neutralize_markers(text: str) -> str:
    """Break HTML-comment delimiters so quoted log text cannot smuggle a
    ``<!-- viberpit:… -->`` marker (or hide content) into an issue body."""
    return text.replace("<!--", "<!\u200b--").replace("-->", "--\u200b>")
```

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest server/tests/test_issue_filing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/issue_filing.py server/tests/test_issue_filing.py
printf 'feat(automations): shared issue_filing decide/apply for fingerprint-to-issue filers\n\nRefs #1433 #1434.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c7.txt
git commit -F /tmp/c7.txt
```

---
### Task 8: `ErrorTriage` automation, policy and issue text — pure parts of `server/automations/error_triage.py`

**Files:**
- Create: `server/automations/error_triage.py`
- Test: `server/tests/test_error_triage_policy.py`

**Interfaces:**
- Consumes: `ConfigField`, `AutomationStatus`, `EnginePolicy` (existing); `neutralize_markers` (Task 7); `ci_issues.BODY_MAX_CHARS`, `ci_issues.issue_url`; `redact_text`.
- Produces:
  - `LABEL = "auto-error"`; `source_label(source: str) -> str` → `"source:client"` for `client`, else `"source:server"`.
  - `class ErrorTriage` — `id = "error-triage"`, `modes = ("off", "file")`, `config_schema` (keys: `target_repo`, `min_count_error`, `min_count_warning`, `warning_window_h`, `daily_cap`, `comment_every_h`, `ignore_loggers`, `retention_days`), `status(ctx)` reading `ctx.error_triage_counts`, `on_state_change` no-op.
  - `should_file(*, level: str, count_since_filed: int, config: dict) -> bool`
  - `is_ignored(logger: str | None, config: dict) -> bool`
  - `issue_title(row, *, regression: bool) -> str`
  - `issue_body(row, *, regression_of: int | None, repo: str) -> str`
  - `recurrence_comment(row, *, since: datetime | None) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_error_triage_policy.py
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

CFG = {"target_repo": "steiner385/viberpit", "min_count_error": 1, "min_count_warning": 3,
       "warning_window_h": 24, "daily_cap": 10, "comment_every_h": 24,
       "ignore_loggers": "uvicorn.error,websockets", "retention_days": 90}


def _row(**kw):
    base = dict(fingerprint="a1b2c3d4e5f60718", source="daemon", level="ERROR",
                logger="server.sessions.orchestrator", event="completion.question_hold",
                title="ERROR completion.question_hold: sid=<hex>",
                sample_message="completion.question_hold {\"sid\": \"abc\"}",
                sample_exc="Traceback…\nValueError: x <!-- sneaky -->",
                sample_context={"host": "dobby", "app_version": "4d2096a5",
                                "recent_sessions": ["s1", "s2"], "proc_epoch": "1-2"},
                count=14, count_since_filed=14, status="observed", issue_number=None,
                first_seen_at=datetime(2026, 9, 7, 9, 12), last_seen_at=datetime(2026, 9, 7, 13, 40),
                filed_at=None, last_commented_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_automation_contract():
    from server.automations.base import Automation, validate_config
    from server.automations.engine_policy import EnginePolicy
    from server.automations.error_triage import ErrorTriage
    a = ErrorTriage()
    assert isinstance(a, Automation)
    assert a.id == "error-triage" and a.modes == ("off", "file")
    assert a.engine_policy == EnginePolicy.CLAUDE_ONLY
    assert validate_config(a.config_schema, {}) == CFG
    assert a.status(None).level == "idle"
    ctx = SimpleNamespace(error_triage_counts={"candidates": 2, "filed_today": 1,
                                               "daily_cap": 10, "muted": 0, "filed": 3})
    st = a.status(ctx)
    assert st.level == "active" and "2 waiting" in st.text
    ctx.error_triage_counts.update(filed_today=10, candidates=4)
    assert a.status(ctx).level == "warn"


@pytest.mark.parametrize("level, n, want", [
    ("ERROR", 0, False), ("ERROR", 1, True), ("CRITICAL", 1, True),
    ("WARNING", 2, False), ("WARNING", 3, True),
])
def test_should_file(level, n, want):
    from server.automations.error_triage import should_file
    assert should_file(level=level, count_since_filed=n, config=CFG) is want


def test_is_ignored_prefix_match():
    from server.automations.error_triage import is_ignored
    assert is_ignored("uvicorn.error", CFG) and is_ignored("websockets.server", CFG)
    assert not is_ignored("server.sessions", CFG) and not is_ignored(None, CFG)
    assert not is_ignored("uvicorn.error", {**CFG, "ignore_loggers": ""})


def test_issue_title_and_labels():
    from server.automations.error_triage import LABEL, issue_title, source_label
    assert issue_title(_row(), regression=False) == \
        "[auto-error] ERROR completion.question_hold: sid=<hex>"
    assert issue_title(_row(), regression=True).startswith("[auto-error][regression] ")
    assert len(issue_title(_row(title="x" * 500), regression=False)) <= 120
    assert LABEL == "auto-error"
    assert source_label("client") == "source:client"
    assert source_label("edge") == source_label("daemon") == "source:server"


def test_issue_body_contents_and_safety():
    from server.automations.error_triage import issue_body
    from server.github.ci_issues import BODY_MAX_CHARS
    body = issue_body(_row(), regression_of=None, repo="steiner385/viberpit")
    assert body.startswith("**Filed by ViberPit Error Triage**")
    assert "| Level / source | ERROR / daemon |" in body
    assert "| Logger / event | server.sessions.orchestrator / completion.question_hold |" in body
    assert "14 since 2026-09-07 09:12 UTC" in body and "last 2026-09-07 13:40 UTC" in body
    assert "s1" in body and "s2" in body and "4d2096a5" in body
    assert "Fingerprint: `a1b2c3d4e5f60718`" in body
    assert "<!--" not in body and "sneaky" in body
    assert "Regression of" not in body
    reg = issue_body(_row(), regression_of=1234, repo="steiner385/viberpit")
    assert "| Regression of | #1234 |" in reg
    huge = issue_body(_row(sample_exc="y" * 20000), regression_of=None, repo="o/r")
    assert len(huge) <= BODY_MAX_CHARS


def test_recurrence_comment():
    from server.automations.error_triage import recurrence_comment
    text = recurrence_comment(_row(count_since_filed=5), since=datetime(2026, 9, 6, 8, 0))
    assert "5 more occurrence(s) since 2026-09-06 08:00 UTC" in text
    assert "last at 2026-09-07 13:40 UTC" in text
    assert "`a1b2c3d4e5f60718`" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_error_triage_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.automations.error_triage'`.

- [ ] **Step 3: Implement the pure half**

```python
# server/automations/error_triage.py
"""The error-triage automation: WARNING+ log signatures → deduplicated backlog
issues (spec docs/superpowers/specs/2026-09-07-error-triage-design.md).

This module has two halves. The pure half (policy + issue text) is here at the
top; ``ErrorTriageLoop`` (the tick) and the action provider follow. Capture
lives in server/obs/errsink.py; the file/comment/regress rule in issue_filing.py.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from ccswitch import gitrepos
from server.audit import redact_text
from server.github import ci_issues

from .base import ActionableItem, ActionSpec, AutomationStatus, ConfigField, TriggerResult
from .engine_policy import EnginePolicy
from .issue_filing import Decision, Linked, apply, decide, neutralize_markers

log = logging.getLogger(__name__)

LABEL = "auto-error"
_TITLE_MAX = 120
_TS = "%Y-%m-%d %H:%M UTC"


def source_label(source: str | None) -> str:
    return "source:client" if source == "client" else "source:server"


class ErrorTriage:
    id = "error-triage"
    name = "Error Triage"
    description = ("Turns ViberPit's own warnings and errors — edge, daemon and "
                   "browser — into deduplicated backlog issues.")
    modes: tuple[str, ...] = ("off", "file")
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY  # never spawns a session
    config_schema = [
        ConfigField(key="target_repo", label="Target repository", type="string",
                    default="steiner385/viberpit", help="Where issues are filed."),
        ConfigField(key="min_count_error", label="ERROR occurrences before filing",
                    type="int", default=1),
        ConfigField(key="min_count_warning", label="WARNING occurrences before filing",
                    type="int", default=3,
                    help="Counted within one burst (see warning window)."),
        ConfigField(key="warning_window_h", label="Warning burst window (hours)",
                    type="int", default=24,
                    help="A WARNING quiet this long without filing starts counting from zero."),
        ConfigField(key="daily_cap", label="New issues per day", type="int", default=10),
        ConfigField(key="comment_every_h", label="Recurrence comment gap (hours)",
                    type="int", default=24),
        ConfigField(key="ignore_loggers", label="Ignored logger prefixes", type="string",
                    default="uvicorn.error,websockets",
                    help="Comma-separated; still counted, never filed."),
        ConfigField(key="retention_days", label="Retention (days)", type="int", default=90,
                    help="Unfiled/resolved signatures not seen this long are deleted."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "error_triage_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        waiting = int(counts.get("candidates", 0))
        filed_today, cap = int(counts.get("filed_today", 0)), int(counts.get("daily_cap", 0))
        bits = [f"{waiting} waiting", f"{filed_today}/{cap} filed today",
                f"{int(counts.get('filed', 0))} open", f"{int(counts.get('muted', 0))} muted"]
        if waiting and cap and filed_today >= cap:
            level = "warn"
        elif waiting or filed_today:
            level = "active"
        else:
            level = "idle"
        return AutomationStatus(text=", ".join(bits), level=level, counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        ...  # the loop reads mode/config each tick


def should_file(*, level: str, count_since_filed: int, config: dict) -> bool:
    """The one filing policy: ERROR/CRITICAL vs WARNING thresholds on
    occurrences since the last file/comment (a stale WARNING burst is reset to
    zero by the tick before this runs)."""
    if level in ("ERROR", "CRITICAL"):
        return count_since_filed >= int(config.get("min_count_error", 1))
    return count_since_filed >= int(config.get("min_count_warning", 3))


def is_ignored(logger: str | None, config: dict) -> bool:
    prefixes = [p.strip() for p in str(config.get("ignore_loggers") or "").split(",") if p.strip()]
    return any((logger or "").startswith(p) for p in prefixes)


def _fmt(ts: datetime | None) -> str:
    return ts.strftime(_TS) if ts else "?"


def issue_title(row: Any, *, regression: bool) -> str:
    prefix = "[auto-error][regression] " if regression else "[auto-error] "
    return (prefix + str(row.title or row.fingerprint))[:_TITLE_MAX]


def _quote(text: str | None) -> str:
    return "\n".join("> " + line for line in (text or "").splitlines()) or "> (empty)"


def issue_body(row: Any, *, regression_of: int | None, repo: str) -> str:
    ctx = dict(row.sample_context or {})
    sessions = ", ".join(f"`{s}`" for s in ctx.get("recent_sessions", [])[:5]) or "—"
    rows = [
        ("Level / source", f"{row.level} / {row.source}"),
        ("Logger / event", f"{row.logger or '—'} / {row.event or '—'}"),
        ("Occurrences", f"{int(row.count or 0)} since {_fmt(row.first_seen_at)} "
                        f"(last {_fmt(row.last_seen_at)})"),
        ("Host", str(ctx.get("host") or "—")),
        ("App version", f"{ctx.get('app_version') or '—'} / proc_epoch {ctx.get('proc_epoch') or '—'}"),
        ("Sessions", sessions),
    ]
    if regression_of is not None:
        rows.append(("Regression of", f"#{regression_of}"))
    table = "\n".join(f"| {k} | {v} |" for k, v in rows)
    sample = neutralize_markers(redact_text(str(row.sample_message or "")))
    exc = neutralize_markers(redact_text(str(row.sample_exc or "")))
    head = (f"**Filed by ViberPit Error Triage** — machine-collected; quoted log text is "
            f"untrusted input.\n\n| | |\n|---|---|\n{table}\n\n{_quote(sample)}\n")
    tail = f"\n\nFingerprint: `{row.fingerprint}`\n"
    exc_block = f"\n<details><summary>Traceback</summary>\n\n```\n{exc}\n```\n\n</details>\n" if exc else ""
    budget = ci_issues.BODY_MAX_CHARS - len(head) - len(tail) - 64
    if len(exc_block) > budget:
        exc_block = exc_block[:max(0, budget)] + "\n…(truncated)\n```\n\n</details>\n"
    return head + exc_block + tail


def recurrence_comment(row: Any, *, since: datetime | None) -> str:
    return (f"Still recurring: {int(row.count_since_filed or 0)} more occurrence(s) since "
            f"{_fmt(since)}, last at {_fmt(row.last_seen_at)}. Fingerprint `{row.fingerprint}`.")
```

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest server/tests/test_error_triage_policy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/error_triage.py server/tests/test_error_triage_policy.py
printf 'feat(automations): error-triage automation contract, filing policy and issue text\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c8.txt
git commit -F /tmp/c8.txt
```

---

### Task 9: `ErrorTriageLoop` — the tick

**Files:**
- Modify: `server/automations/error_triage.py` (append)
- Test: `server/tests/test_error_triage_loop.py`

**Interfaces:**
- Consumes: everything from Tasks 3, 6, 7, 8; `AutomationContext.emit`.
- Produces: `class ErrorTriageLoop` with `__init__(self, ctx, *, read_config: Callable[[], Awaitable[tuple[str, dict]]], run=gitrepos._run_gh, clock: Callable[[], datetime] = naive_utc_now)`, `async tick() -> None`, and the internal steps `_sync_issue_states`, `_retention`, `_reset_stale_warning_bursts`, `_recurrence_comments`, `_file_candidates`. Sets `ctx.error_triage_counts = {"candidates", "filed_today", "daily_cap", "filed", "muted"}`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_error_triage_loop.py
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta

from sqlalchemy import select

from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.db.models import ErrorSignature

NOW = datetime(2026, 9, 7, 12, 0, 0)
CFG = {"target_repo": "o/r", "min_count_error": 1, "min_count_warning": 3,
       "warning_window_h": 24, "daily_cap": 10, "comment_every_h": 24,
       "ignore_loggers": "uvicorn.error,websockets", "retention_days": 90}


class FakeGh:
    """Answers gh calls: label ensure, issue create (incrementing numbers, optional
    failure), comments, and one GraphQL issue-state query."""
    def __init__(self):
        self.calls: list[list[str]] = []
        self.next_number = 42
        self.fail_create = False
        self.states: dict[int, tuple[bool, str | None]] = {}

    def __call__(self, args, *a, **k):
        self.calls.append(list(args))
        path = args[1] if len(args) > 1 else ""
        if path == "graphql":
            data = {f"i{n}": {"number": n, "closed": c, "stateReason": (r or "").upper() or None}
                    for n, (c, r) in self.states.items()}
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"repository": data}}), "")
        if path.endswith("/labels"):
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if path.endswith("/comments"):
            return subprocess.CompletedProcess(args, 0, json.dumps({"html_url": "https://x/c"}), "")
        if self.fail_create:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        n, self.next_number = self.next_number, self.next_number + 1
        return subprocess.CompletedProcess(args, 0, json.dumps({"number": n}), "")

    @property
    def creates(self):
        return [c for c in self.calls if c[1] == "repos/o/r/issues"]

    @property
    def comments(self):
        return [c for c in self.calls if c[1].endswith("/comments")]


def _loop(ctx, gh, *, mode="file", config=None, now=NOW):
    from server.automations.error_triage import ErrorTriageLoop
    async def read_config():
        return mode, {**CFG, **(config or {})}
    return ErrorTriageLoop(ctx, read_config=read_config, run=gh, clock=lambda: now)


async def _seed(**kw):
    base = dict(fingerprint="fp1", source="daemon", level="ERROR", logger="server.x",
                title="ERROR server.x: boom", sample_message="boom", count=1,
                count_since_filed=1, status="observed", sample_context={"recent_sessions": []},
                first_seen_at=NOW - timedelta(hours=1), last_seen_at=NOW - timedelta(minutes=1))
    base.update(kw)
    async with db_session() as db:
        db.add(ErrorSignature(**base))
    return base["fingerprint"]


async def _row(fp):
    async with db_session() as db:
        return (await db.execute(select(ErrorSignature).where(
            ErrorSignature.fingerprint == fp))).scalars().one()


def _run(coro_fn, tmp_path):
    async def go():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await coro_fn()
    asyncio.run(go())


def test_off_mode_files_nothing(tmp_path):
    async def body():
        await _seed()
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh, mode="off").tick()
        assert gh.calls == [] and (await _row("fp1")).status == "observed"
    _run(body, tmp_path)


def test_error_files_on_first_occurrence_two_phase(tmp_path):
    async def body():
        await _seed(count_since_filed=3, count=3)
        gh = FakeGh()
        ctx = AutomationContext("error-triage")
        await _loop(ctx, gh).tick()
        row = await _row("fp1")
        assert row.status == "filed" and row.issue_number == 42
        assert row.filed_at == NOW and row.count_since_filed == 0
        create = gh.creates[0]
        assert "labels[]=auto-error" in create and "labels[]=source:server" in create
        assert any("title=[auto-error] ERROR server.x: boom" == c for c in create)
        assert ctx.error_triage_counts["filed"] == 1
    _run(body, tmp_path)


def test_warning_threshold_and_stale_burst_reset(tmp_path):
    async def body():
        await _seed(fingerprint="w2", level="WARNING", count_since_filed=2)
        await _seed(fingerprint="w3", level="WARNING", count_since_filed=3)
        await _seed(fingerprint="stale", level="WARNING", count_since_filed=2,
                    last_seen_at=NOW - timedelta(hours=30))
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert (await _row("w2")).status == "observed"
        assert (await _row("w3")).status == "filed"
        assert (await _row("stale")).count_since_filed == 0
        assert len(gh.creates) == 1
    _run(body, tmp_path)


def test_ignored_logger_and_invalid_repo(tmp_path):
    async def body():
        await _seed(logger="uvicorn.error")
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert gh.creates == []
        await _seed(fingerprint="fp2")
        await _loop(AutomationContext("error-triage"), gh, config={"target_repo": "../x"}).tick()
        assert gh.calls == []
    _run(body, tmp_path)


def test_daily_cap_counts_todays_filings(tmp_path):
    async def body():
        await _seed(fingerprint="a", last_seen_at=NOW - timedelta(minutes=5))
        await _seed(fingerprint="b", last_seen_at=NOW - timedelta(minutes=1))
        await _seed(fingerprint="old", status="filed", issue_number=9,
                    filed_at=NOW - timedelta(days=1, minutes=1), count_since_filed=0)
        gh = FakeGh()
        ctx = AutomationContext("error-triage")
        await _loop(ctx, gh, config={"daily_cap": 1}).tick()
        assert (await _row("b")).status == "filed"      # newest first
        assert (await _row("a")).status == "observed"
        assert ctx.error_triage_counts["candidates"] == 1
        assert ctx.error_triage_counts["filed_today"] == 1
    _run(body, tmp_path)


def test_failed_create_is_retried_next_tick(tmp_path):
    async def body():
        await _seed()
        gh = FakeGh()
        gh.fail_create = True
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        row = await _row("fp1")
        assert row.status == "filed" and row.issue_number is None
        gh.fail_create = False
        await loop.tick()
        row = await _row("fp1")
        assert row.issue_number == 42 and len(gh.creates) == 2
    _run(body, tmp_path)


def test_recurrence_comment_then_throttle(tmp_path):
    async def body():
        await _seed(status="filed", issue_number=42, filed_at=NOW - timedelta(hours=3),
                    count_since_filed=5, count=6)
        gh = FakeGh()
        gh.states = {42: (False, None)}
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        row = await _row("fp1")
        assert len(gh.comments) == 1 and row.count_since_filed == 0
        assert row.last_commented_at == NOW
        assert any("5 more occurrence(s)" in part for part in gh.comments[0])
        async with db_session() as db:
            (await db.execute(select(ErrorSignature))).scalars().one().count_since_filed = 2
        await loop.tick()
        assert len(gh.comments) == 1  # throttled: same clock, <24h since the comment
        assert gh.creates == []
    _run(body, tmp_path)


def test_closed_completed_resolves_then_regression_links_old(tmp_path):
    async def body():
        await _seed(status="filed", issue_number=41, filed_at=NOW - timedelta(days=2),
                    count_since_filed=0)
        gh = FakeGh()
        gh.states = {41: (True, "completed")}
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        row = await _row("fp1")
        assert row.status == "resolved" and row.issue_closed_at == NOW
        assert gh.creates == []
        async with db_session() as db:
            r = (await db.execute(select(ErrorSignature))).scalars().one()
            r.count_since_filed, r.count = 1, 2
        await loop.tick()
        row = await _row("fp1")
        assert row.status == "filed" and row.issue_number == 42
        create = gh.creates[0]
        assert any(c.startswith("title=[auto-error][regression] ") for c in create)
        assert any("| Regression of | #41 |" in c for c in create)
    _run(body, tmp_path)


def test_closed_not_planned_mutes_forever(tmp_path):
    async def body():
        await _seed(status="filed", issue_number=42, filed_at=NOW - timedelta(days=2),
                    count_since_filed=7)
        gh = FakeGh()
        gh.states = {42: (True, "not_planned")}
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        row = await _row("fp1")
        assert row.status == "muted" and row.muted_by == "github:not_planned"
        assert gh.comments == [] and gh.creates == []
        await loop.tick()
        assert gh.creates == []
    _run(body, tmp_path)


def test_retention_deletes_stale_unfiled_only(tmp_path):
    async def body():
        old = NOW - timedelta(days=91)
        await _seed(fingerprint="obs", last_seen_at=old, count_since_filed=0)
        await _seed(fingerprint="res", status="resolved", issue_number=1, last_seen_at=old,
                    count_since_filed=0)
        await _seed(fingerprint="fil", status="filed", issue_number=2, last_seen_at=old,
                    count_since_filed=0)
        await _seed(fingerprint="mut", status="muted", last_seen_at=old, count_since_filed=0)
        gh = FakeGh()
        gh.states = {2: (False, None)}
        await _loop(AutomationContext("error-triage"), gh).tick()
        async with db_session() as db:
            left = set((await db.execute(select(ErrorSignature.fingerprint))).scalars().all())
        assert left == {"fil", "mut"}
    _run(body, tmp_path)
```

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_error_triage_loop.py -v`
Expected: FAIL — `ImportError: cannot import name 'ErrorTriageLoop'`.

- [ ] **Step 3: Append the loop to `server/automations/error_triage.py`**

```python
# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    from server.db.clock import naive_utc_now
    return naive_utc_now()


class ErrorTriageLoop:
    """Restart-safe, non-reentrant tick. Every I/O seam is injected: ``run`` is
    the gh runner (``gitrepos._run_gh``), ``clock`` the naive-UTC now."""

    def __init__(self, ctx, *,
                 read_config: Callable[[], Awaitable[tuple[str, dict]]],
                 run=gitrepos._run_gh,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._run = run
        self._clock = clock
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run_once()
            except Exception:  # noqa: BLE001 — a tick must never crash the loop
                log.warning("error_triage.tick_failed", exc_info=True)

    async def _run_once(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        repo = str(config.get("target_repo") or "")
        if not ci_issues.valid_repo(repo):
            log.warning("error_triage.invalid_target_repo repo=%r", repo)
            return
        now = self._clock()
        await self._sync_issue_states(repo, now)
        await self._retention(config, now)
        await self._reset_stale_warning_bursts(config, now)
        await self._recurrence_comments(repo, config, now)
        candidates, filed_today = await self._file_candidates(repo, config, now)
        setattr(self._ctx, "error_triage_counts", {
            **(await self._counts()), "candidates": candidates,
            "filed_today": filed_today, "daily_cap": int(config.get("daily_cap", 0))})

    # -- steps ---------------------------------------------------------------
    async def _sync_issue_states(self, repo: str, now: datetime) -> None:
        from sqlalchemy import select, update
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        async with db_session() as db:
            numbers = [int(n) for n in (await db.execute(select(ES.issue_number).where(
                ES.status == "filed", ES.issue_number.is_not(None)))).scalars().all()]
        if not numbers:
            return
        states = await asyncio.to_thread(ci_issues.issue_states, repo, numbers, run=self._run)
        for number, (closed, reason) in states.items():
            if not closed:
                continue
            async with db_session() as db:
                if reason == "not_planned":
                    await db.execute(update(ES).where(ES.issue_number == number, ES.status == "filed")
                                     .values(status="muted", muted_at=now,
                                             muted_by="github:not_planned"))
                else:
                    await db.execute(update(ES).where(ES.issue_number == number, ES.status == "filed")
                                     .values(status="resolved", issue_closed_at=now))

    async def _retention(self, config: dict, now: datetime) -> None:
        from sqlalchemy import delete
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        cutoff = now - timedelta(days=int(config.get("retention_days", 90)))
        async with db_session() as db:
            await db.execute(delete(ES).where(ES.status.in_(("observed", "resolved")),
                                              ES.last_seen_at < cutoff))

    async def _reset_stale_warning_bursts(self, config: dict, now: datetime) -> None:
        from sqlalchemy import update
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        cutoff = now - timedelta(hours=int(config.get("warning_window_h", 24)))
        async with db_session() as db:
            await db.execute(update(ES).where(
                ES.status == "observed", ES.level == "WARNING",
                ES.last_seen_at < cutoff, ES.count_since_filed > 0
            ).values(count_since_filed=0))

    async def _recurrence_comments(self, repo: str, config: dict, now: datetime) -> None:
        from sqlalchemy import select, update
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        every = timedelta(hours=int(config.get("comment_every_h", 24)))
        async with db_session() as db:
            rows = (await db.execute(select(ES).where(
                ES.status == "filed", ES.issue_number.is_not(None),
                ES.count_since_filed > 0))).scalars().all()
            plan = []
            for r in rows:
                linked = Linked(number=r.issue_number, closed=False,
                                last_commented_at=r.last_commented_at)
                d = decide(linked, cap_remaining=1, comment_every=every, now=now)
                if d.kind == "comment":
                    since = r.last_commented_at or r.filed_at
                    plan.append((r.fingerprint, linked, int(r.count_since_filed or 0),
                                 recurrence_comment(r, since=since)))
        for fp, linked, n, body in plan:
            applied = await apply(Decision("comment", "open"), repo=repo, title="", body=body,
                                  labels=(), linked=linked, run=self._run)
            if not applied.commented:
                continue  # retried next tick; last_commented_at only moves on success
            async with db_session() as db:
                await db.execute(update(ES).where(ES.fingerprint == fp).values(
                    count_since_filed=ES.count_since_filed - n, last_commented_at=now))
            await self._ctx.emit("info", f"commented on #{linked.number}: {n} more occurrence(s)",
                                 kind="commented", target=fp)

    async def _file_candidates(self, repo: str, config: dict, now: datetime) -> tuple[int, int]:
        """Returns (candidates still waiting after this tick, filed today)."""
        from sqlalchemy import func, select, update
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cap = int(config.get("daily_cap", 10))
        every = timedelta(hours=int(config.get("comment_every_h", 24)))
        plan: list[tuple[str, Decision, str, str, list[str]]] = []
        waiting = 0
        async with db_session() as db:
            filed_today = int((await db.execute(select(func.count()).select_from(ES).where(
                ES.filed_at >= day_start))).scalar() or 0)
            cap_remaining = max(0, cap - filed_today)
            # A prior create failed (row claimed, no issue): retry first, outside the cap —
            # its filed_at already counted toward today.
            retry = (await db.execute(select(ES).where(
                ES.status == "filed", ES.issue_number.is_(None)))).scalars().all()
            for r in retry:
                plan.append((r.fingerprint, Decision("file", "retry"),
                             issue_title(r, regression=False),
                             issue_body(r, regression_of=None, repo=repo),
                             [LABEL, source_label(r.source)]))
            rows = (await db.execute(select(ES).where(ES.status.in_(("observed", "resolved")))
                                     .order_by(ES.last_seen_at.desc()))).scalars().all()
            for r in rows:
                if is_ignored(r.logger, config) or not should_file(
                        level=str(r.level or ""), count_since_filed=int(r.count_since_filed or 0),
                        config=config):
                    continue
                linked = (Linked(number=r.issue_number, closed=True, closed_reason="completed")
                          if r.status == "resolved" and r.issue_number else None)
                d = decide(linked, cap_remaining=cap_remaining, comment_every=every, now=now)
                if d.kind not in ("file", "regress"):
                    waiting += 1
                    continue
                seen = int(r.count_since_filed or 0)
                regression_of = r.issue_number if d.kind == "regress" else None
                plan.append((r.fingerprint, d, issue_title(r, regression=d.kind == "regress"),
                             issue_body(r, regression_of=regression_of, repo=repo),
                             [LABEL, source_label(r.source)]))
                # Phase 1: claim the row. count_since_filed is decremented (not zeroed)
                # so occurrences the sink adds concurrently are not lost.
                await db.execute(update(ES).where(ES.fingerprint == r.fingerprint).values(
                    status="filed", filed_at=now, issue_number=None, issue_closed_at=None,
                    count_since_filed=ES.count_since_filed - seen))
                cap_remaining -= 1
                filed_today += 1
        # Phase 2: gh, outside the DB write lock.
        for fp, d, title, body, labels in plan:
            applied = await apply(d, repo=repo, title=title, body=body, labels=labels,
                                  linked=None, run=self._run)
            if applied.issue_number is None:
                await self._ctx.emit("warn", f"issue create failed — will retry: {title}",
                                     kind="file_failed", target=fp)
                continue
            async with db_session() as db:
                await db.execute(update(ES).where(ES.fingerprint == fp)
                                 .values(issue_number=applied.issue_number))
            await self._ctx.emit("info", f"filed #{applied.issue_number}: {title}",
                                 kind="regressed" if d.kind == "regress" else "filed",
                                 target=fp, detail={"url": ci_issues.issue_url(repo, applied.issue_number)})
        return waiting, filed_today

    async def _counts(self) -> dict[str, int]:
        from sqlalchemy import func, select
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        async with db_session() as db:
            rows = (await db.execute(select(ES.status, func.count()).group_by(ES.status))).all()
        by = {str(s): int(n) for s, n in rows}
        return {"filed": by.get("filed", 0), "muted": by.get("muted", 0)}
```

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest server/tests/test_error_triage_loop.py server/tests/test_error_triage_policy.py -v`
Expected: PASS. If `test_daily_cap_counts_todays_filings` fails on `filed_today == 1`: the pre-existing `old` row's `filed_at` is a day and a minute before `NOW` and must not count; check `day_start` is midnight of `NOW`'s date, not `NOW - 24h`.

- [ ] **Step 5: Commit**

```bash
git add server/automations/error_triage.py server/tests/test_error_triage_loop.py
printf 'feat(automations): ErrorTriageLoop — sync, retention, recurrence comments, two-phase filing\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c9.txt
git commit -F /tmp/c9.txt
```

---
### Task 10: Card actions — File now / Mute / Unmute

**Files:**
- Modify: `server/automations/error_triage.py` (append)
- Test: `server/tests/test_error_triage_actions.py`

**Interfaces:**
- Consumes: `ErrorTriageLoop` (Task 9), `ActionableItem`, `ActionSpec`, `TriggerResult`.
- Produces:
  - `ErrorTriageLoop.actionable_items(limit: int = 50) -> list[ActionableItem]`
  - `ErrorTriageLoop.action(fingerprint: str, kind: str) -> str` → status string `"filed" | "muted" | "unmuted" | "not_found" | "failed" | "off"`
  - `class ErrorTriageActionProvider` with `valid_kinds = frozenset({"file_now", "mute", "unmute"})`, `__init__(self, orch)`, `actionable()`, `trigger(item_id, kind)`. Reads `orch._error_triage_loop` lazily.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_error_triage_actions.py
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from server.automations.context import AutomationContext
from server.db import init_db
from server.tests.test_error_triage_loop import CFG, NOW, FakeGh, _loop, _row, _seed


def _run(coro_fn, tmp_path):
    async def go():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await coro_fn()
    asyncio.run(go())


def test_actionable_lists_candidates_filed_and_muted(tmp_path):
    async def body():
        await _seed(fingerprint="cand", level="WARNING", count_since_filed=3)
        await _seed(fingerprint="below", level="WARNING", count_since_filed=1)
        await _seed(fingerprint="filed", status="filed", issue_number=42, count_since_filed=0)
        await _seed(fingerprint="muted", status="muted", muted_by="operator", count_since_filed=0)
        loop = _loop(AutomationContext("error-triage"), FakeGh())
        items = {it.id: it for it in await loop.actionable_items()}
        assert set(items) == {"cand", "filed", "muted"}
        assert {a.kind for a in items["cand"].actions} == {"file_now", "mute"}
        assert {a.kind for a in items["filed"].actions} == {"mute"}
        assert items["filed"].url == "https://github.com/o/r/issues/42"
        assert {a.kind for a in items["muted"].actions} == {"unmute"}
    _run(body, tmp_path)


def test_file_now_bypasses_threshold_and_cap(tmp_path):
    async def body():
        await _seed(fingerprint="w1", level="WARNING", count_since_filed=1)
        gh = FakeGh()
        loop = _loop(AutomationContext("error-triage"), gh, config={"daily_cap": 0})
        assert await loop.action("w1", "file_now") == "filed"
        row = await _row("w1")
        assert row.status == "filed" and row.issue_number == 42 and row.filed_at == NOW
        assert await loop.action("nope", "file_now") == "not_found"
    _run(body, tmp_path)


def test_file_now_reports_failure_and_stays_retryable(tmp_path):
    async def body():
        await _seed()
        gh = FakeGh()
        gh.fail_create = True
        loop = _loop(AutomationContext("error-triage"), gh)
        assert await loop.action("fp1", "file_now") == "failed"
        row = await _row("fp1")
        assert row.status == "filed" and row.issue_number is None
    _run(body, tmp_path)


def test_mute_and_unmute(tmp_path):
    async def body():
        await _seed(fingerprint="a")
        await _seed(fingerprint="b", status="filed", issue_number=7, count_since_filed=0)
        loop = _loop(AutomationContext("error-triage"), FakeGh())
        assert await loop.action("a", "mute") == "muted"
        row = await _row("a")
        assert row.status == "muted" and row.muted_by == "operator" and row.muted_at == NOW
        assert await loop.action("a", "unmute") == "unmuted"
        assert (await _row("a")).status == "observed"
        assert await loop.action("b", "mute") == "muted"
        assert await loop.action("b", "unmute") == "unmuted"
        assert (await _row("b")).status == "filed"  # still has an issue → back to filed
        assert await loop.action("a", "bogus") == "failed"
    _run(body, tmp_path)


def test_file_now_when_off(tmp_path):
    async def body():
        await _seed()
        loop = _loop(AutomationContext("error-triage"), FakeGh(), mode="off")
        assert await loop.action("fp1", "file_now") == "off"
    _run(body, tmp_path)


def test_action_provider_maps_statuses(tmp_path):
    async def body():
        from server.automations.error_triage import ErrorTriageActionProvider
        await _seed()
        loop = _loop(AutomationContext("error-triage"), FakeGh())
        provider = ErrorTriageActionProvider(SimpleNamespace(_error_triage_loop=loop))
        assert provider.valid_kinds == {"file_now", "mute", "unmute"}
        items = await provider.actionable()
        assert items and items[0].id == "fp1"
        res = await provider.trigger("fp1", "file_now")
        assert res.status == "filed" and res.level == "ok" and "#42" in res.message
        res = await provider.trigger("zzz", "mute")
        assert res.status == "not_found" and res.level == "warn"
    _run(body, tmp_path)
```

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_error_triage_actions.py -v`
Expected: FAIL — `AttributeError: 'ErrorTriageLoop' object has no attribute 'actionable_items'`.

- [ ] **Step 3: Append to `server/automations/error_triage.py`**

Add these methods inside `ErrorTriageLoop` (after `_counts`):

```python
    # -- card actions ---------------------------------------------------------
    async def actionable_items(self, limit: int = 50) -> list[ActionableItem]:
        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        _, config = await self._read_config()
        repo = str(config.get("target_repo") or "")
        async with db_session() as db:
            rows = (await db.execute(select(ES).where(ES.status.in_(("observed", "resolved", "filed", "muted")))
                                     .order_by(ES.last_seen_at.desc()).limit(limit * 4))).scalars().all()
        items: list[ActionableItem] = []
        for r in rows:
            subtitle = f"{r.level} · {r.source} · {int(r.count or 0)}× · last {_fmt(r.last_seen_at)}"
            url = ci_issues.issue_url(repo, int(r.issue_number)) if r.issue_number and repo else None
            if r.status == "muted":
                actions = [ActionSpec(kind="unmute", label="Unmute")]
            elif r.status == "filed":
                actions = [ActionSpec(kind="mute", label="Mute", style="risky")]
            else:
                if is_ignored(r.logger, config) or not should_file(
                        level=str(r.level or ""), count_since_filed=int(r.count_since_filed or 0),
                        config=config):
                    continue
                actions = [ActionSpec(kind="file_now", label="File now"),
                           ActionSpec(kind="mute", label="Mute", style="risky")]
            items.append(ActionableItem(id=str(r.fingerprint), title=str(r.title or r.fingerprint),
                                        subtitle=subtitle, url=url, actions=actions))
            if len(items) >= limit:
                break
        return items

    async def action(self, fingerprint: str, kind: str) -> str:
        from sqlalchemy import select, update
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        mode, config = await self._read_config()
        now = self._clock()
        async with db_session() as db:
            row = (await db.execute(select(ES).where(ES.fingerprint == fingerprint))).scalars().one_or_none()
            if row is None:
                return "not_found"
            if kind == "mute":
                await db.execute(update(ES).where(ES.fingerprint == fingerprint).values(
                    status="muted", muted_at=now, muted_by="operator"))
                return "muted"
            if kind == "unmute":
                back = "filed" if row.issue_number else "observed"
                await db.execute(update(ES).where(ES.fingerprint == fingerprint).values(
                    status=back, muted_at=None, muted_by=None))
                return "unmuted"
            if kind != "file_now":
                return "failed"
            if mode == "off":
                return "off"
            repo = str(config.get("target_repo") or "")
            if not ci_issues.valid_repo(repo) or row.status not in ("observed", "resolved", "filed"):
                return "failed"
            regression_of = row.issue_number if row.status == "resolved" else None
            title = issue_title(row, regression=regression_of is not None)
            body = issue_body(row, regression_of=regression_of, repo=repo)
            labels = [LABEL, source_label(row.source)]
            seen = int(row.count_since_filed or 0)
            await db.execute(update(ES).where(ES.fingerprint == fingerprint).values(
                status="filed", filed_at=now, issue_number=None, issue_closed_at=None,
                count_since_filed=ES.count_since_filed - seen))
        applied = await apply(Decision("regress" if regression_of else "file", "no_issue"),
                              repo=repo, title=title, body=body, labels=labels, linked=None,
                              run=self._run)
        if applied.issue_number is None:
            await self._ctx.emit("warn", f"File now failed — will retry: {title}",
                                 kind="file_failed", target=fingerprint)
            return "failed"
        async with db_session() as db:
            await db.execute(update(ES).where(ES.fingerprint == fingerprint)
                             .values(issue_number=applied.issue_number))
        self._last_filed_number = applied.issue_number
        await self._ctx.emit("info", f"filed #{applied.issue_number} (File now): {title}",
                             kind="filed", target=fingerprint,
                             detail={"url": ci_issues.issue_url(repo, applied.issue_number)})
        return "filed"
```

Add `self._last_filed_number: int | None = None` to `ErrorTriageLoop.__init__`.

Then append the provider at module level:

```python
_ACTION_STATUS = {
    "filed": ("ok", "Issue filed"),
    "muted": ("ok", "Muted — it will not be filed or commented on again"),
    "unmuted": ("ok", "Unmuted"),
    "not_found": ("warn", "That signature no longer exists"),
    "off": ("warn", "Error Triage is off — enable it first"),
    "failed": ("error", "Action failed — see the feed"),
}


class ErrorTriageActionProvider:
    """Card actions for error-triage. Reads the loop off the orchestrator lazily —
    the providers dict is built before the loops are (orchestrator.__init__ order)."""

    valid_kinds = frozenset({"file_now", "mute", "unmute"})

    def __init__(self, orch) -> None:
        self._orch = orch

    @property
    def _loop(self) -> "ErrorTriageLoop":
        return self._orch._error_triage_loop

    async def actionable(self) -> list[ActionableItem]:
        return await self._loop.actionable_items()

    async def trigger(self, item_id: str, kind: str) -> TriggerResult:
        status = await self._loop.action(item_id, kind)
        level, message = _ACTION_STATUS.get(status, ("error", "Action failed — see the feed"))
        if status == "filed" and self._loop._last_filed_number:
            message = f"Issue filed: #{self._loop._last_filed_number}"
        return TriggerResult(status=status, level=level, message=message)
```

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest server/tests/test_error_triage_actions.py server/tests/test_error_triage_loop.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/error_triage.py server/tests/test_error_triage_actions.py
printf 'feat(automations): error-triage card actions — File now, Mute, Unmute\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c10.txt
git commit -F /tmp/c10.txt
```

---

### Task 11: Register the automation and wire the orchestrator loop

**Files:**
- Modify: `server/automations/registry.py:55-82`
- Modify: `server/sessions/orchestrator.py` — `_PRIMARY_ONLY_LOOP_ATTRS` (~line 532), `_action_providers` (~line 861), loop construction next to `_cicd_loop` (~line 1141-1163), `start()` primary-only block (~line 1793), teardown tuple (~line 2138), `automation_statuses` ctx map (~line 10222), and new `_error_triage_*` methods next to `_cicd_tick_once` (~line 7061).
- Test: `server/tests/test_error_triage_wiring.py`

**Interfaces:**
- Consumes: `ErrorTriage`, `ErrorTriageLoop`, `ErrorTriageActionProvider` (Tasks 8–10).
- Produces: orchestrator attributes `_error_triage_ctx`, `_error_triage_loop`, `_error_triage_task`; methods `_error_triage_tick_loop(interval)`, `_error_triage_tick_once()`, `_error_triage_read_config()`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_error_triage_wiring.py
from __future__ import annotations


def test_error_triage_is_registered():
    from server.automations.registry import registry
    a = registry.get("error-triage")
    assert a.id == "error-triage" and a.modes == ("off", "file")


def test_error_triage_loop_is_primary_only_and_torn_down():
    import inspect
    from server.sessions import orchestrator as mod
    assert "_error_triage_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    src = inspect.getsource(mod.SessionOrchestrator.stop)
    assert '"_error_triage_task"' in src, "stop() must reap the error-triage loop"


def test_error_triage_has_action_provider_and_status_ctx():
    from server.sessions.orchestrator import SessionOrchestrator
    import inspect
    init_src = inspect.getsource(SessionOrchestrator.__init__)
    assert '"error-triage": ErrorTriageActionProvider(self)' in init_src
    statuses_src = inspect.getsource(SessionOrchestrator.automation_statuses)
    assert '"error-triage": getattr(self, "_error_triage_ctx", None)' in statuses_src
```

(The existing `test_primary_lease.py::test_primary_start_runs_every_fleet_global_loop` and
`test_worker_start_skips_fleet_global_loops` cover the real start/stop behaviour once the attr is
in `_PRIMARY_ONLY_LOOP_ATTRS`; source assertions here just pin the three wiring points that no
behavioural test reaches cheaply.)

- [ ] **Step 2: Run to verify failure**

Run: `$PY -m pytest server/tests/test_error_triage_wiring.py -v`
Expected: FAIL — `KeyError: 'error-triage'`.

- [ ] **Step 3: Register** — in `server/automations/registry.py` add after the `GhReconcile` import and registration:

```python
from .error_triage import ErrorTriage  # noqa: E402
```
```python
registry.register(ErrorTriage())
```

- [ ] **Step 4: Wire the orchestrator**

(a) `_PRIMARY_ONLY_LOOP_ATTRS`: add `"_error_triage_task",` to the set.

(b) `_action_providers` (~line 856): extend the import and the dict:

```python
        from server.automations.action_providers import (
            PrDoctorActionProvider, BacklogRunnerActionProvider,
            CicdDoctorActionProvider,
        )
        from server.automations.error_triage import ErrorTriageActionProvider
        from server.automations.base import ActionProvider
        self._action_providers: dict[str, ActionProvider] = {
            "pr-doctor": PrDoctorActionProvider(self),
            "backlog-runner": BacklogRunnerActionProvider(self),
            "cicd-doctor": CicdDoctorActionProvider(self),
            "error-triage": ErrorTriageActionProvider(self),
        }
```

(c) Loop construction, right after `self._cicd_task: asyncio.Task | None = None` (~line 1163):

```python
        # Error Triage: WARNING+ log signatures → deduplicated backlog issues
        # (spec 2026-09-07). Filing goes through the ambient gh path like the doctor.
        from server.automations.error_triage import ErrorTriageLoop
        self._error_triage_ctx = AutomationContext("error-triage")
        self._error_triage_loop = ErrorTriageLoop(
            self._error_triage_ctx, read_config=self._error_triage_read_config)
        self._error_triage_task: asyncio.Task | None = None
```

(d) `start()` primary-only block, after the `_cicd_task` creation:

```python
            # Error Triage: 60 s — issue state sync is one GraphQL call, filing is
            # capped per day, so a fast cadence costs little and keeps the inbox fresh.
            self._error_triage_task = asyncio.create_task(
                self._error_triage_tick_loop(interval=60.0), name="error-triage-loop"
            )
```

(e) Teardown tuple in `stop()`: add `"_error_triage_task",` after `"_cicd_task",`.

(f) `automation_statuses` ctx map (~line 10222): add `"error-triage": getattr(self, "_error_triage_ctx", None),`.

(g) Methods, placed right after `_cicd_list_repos`:

```python
    # ------------------------------------------------------------------
    # Error Triage control loop
    # ------------------------------------------------------------------
    async def _error_triage_tick_loop(self, interval: float) -> None:
        while True:
            await self._error_triage_tick_once()
            await asyncio.sleep(interval)

    async def _error_triage_tick_once(self) -> None:
        if not await self._schedule_allows_tick("error-triage"):
            return
        await self._error_triage_loop.tick()
        try:
            mode, _ = await self._error_triage_read_config()
            if mode != "off":
                await self._bump_automation_active("error-triage")
        except Exception:
            log.debug("error_triage.heartbeat_failed", exc_info=True)

    async def _error_triage_read_config(self) -> tuple[str, dict]:
        """(effective mode, validated config); off + defaults on any bad row."""
        from server.automations.error_triage import ErrorTriage
        from server.automations.config_store import read_automation_config
        active = tuple(m for m in ErrorTriage.modes if m != "off")
        return await read_automation_config(
            "error-triage", ErrorTriage.config_schema, modes=active, default_mode=active[0],
            log_key="error_triage")
```

- [ ] **Step 5: Run the wiring, lease and registry tests**

Run: `$PY -m pytest server/tests/test_error_triage_wiring.py server/tests/test_primary_lease.py server/tests/test_automation_registry.py server/tests/test_api_automations*.py -v`
Expected: PASS. If an API test enumerates automation ids or counts cards, add `error-triage` to its expected list.

- [ ] **Step 6: Commit**

```bash
git add server/automations/registry.py server/sessions/orchestrator.py server/tests/test_error_triage_wiring.py
printf 'feat(automations): register error-triage and run its loop on the primary daemon\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c11.txt
git commit -F /tmp/c11.txt
```

---

### Task 12: Spec deltas, full gate, PR

**Files:**
- Modify: `docs/superpowers/specs/2026-09-07-error-triage-design.md` (§4, §5, §7)
- Modify: `docs/superpowers/plans/2026-09-07-error-triage.md` (tick the boxes)

- [ ] **Step 1: Record the implementation deltas in the spec**

Append a section at the end of the spec:

```markdown
## 11. Implementation deltas (recorded 2026-09-07)

- `error_signatures.sample_at` was added so "refresh the sample if older than 24 h" has a
  column to compare against (§4).
- Client fingerprints strip the Vite content hash from asset basenames
  (`index-Bx9k2L3f.js` → `index.js`) as well as line:col (§5) — the hash changes per build.
- Card actions record `muted_by = "operator"`; the trigger seam carries no identity, and
  threading one through the RPC layer was out of scope (§7). GitHub-native mutes still
  record `github:not_planned`.
- The sink's re-entrancy guard covers `emit()`; the async flush is not guarded, so a
  WARNING logged by the DB layer *during* a flush is captured as an ordinary signature
  (bounded by dedupe) rather than dropped (§4).
- `Decision.reason` gained `"retry"` for a row whose earlier create failed (§6.1).
```

- [ ] **Step 2: Run the four-step gate** (from the worktree root, env scrubbed as the memory note says)

```bash
env -u CCSWITCH_SESSIOND_CERT -u CCSWITCH_SESSIOND_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 bash -c '
  $PY -m ruff check src/ server/ &&
  $PY -m mypy server/ --ignore-missing-imports &&
  $PY -m pytest tests/ -q &&
  $PY -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q' 2>&1 | tee /tmp/error-triage-gate.log
```

Acquire the test slot first if running on rocky (CLAUDE.md snippet; put the pytest in the same shell). Expected: all four green. Fix anything red before continuing — mypy typically flags `Mapped[int | None]` arithmetic (`ES.count_since_filed - seen` is fine as a SQL expression; Python-side `int(x or 0)` casts are already in the code).

- [ ] **Step 3: Commit the docs, push, open the PR, arm auto-merge**

```bash
git add docs/superpowers/specs/2026-09-07-error-triage-design.md docs/superpowers/plans/2026-09-07-error-triage.md
printf 'docs(error-triage): record implementation deltas; tick the plan\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c12.txt
git commit -F /tmp/c12.txt
git push -u origin HEAD
cat > /tmp/error-triage-pr.md <<'EOF'
## Summary

Implements `docs/superpowers/specs/2026-09-07-error-triage-design.md` (plan: `docs/superpowers/plans/2026-09-07-error-triage.md`).

- `server/obs/errsink.py`: a WARNING+ `logging.Handler` installed by `configure_logging()` folds records by fingerprint (`server/obs/error_signature.py`) and upserts `error_signatures` every 5 s. Frontend errors ride the existing `/api/client-logs` replay, so one handler covers both sides.
- `error-triage` automation (`server/automations/error_triage.py`): sync issue state (one GraphQL call), reset stale WARNING bursts, comment on recurrence (≤1/day), file new signatures two-phase under a daily cap; closed-as-completed recurrences file a linked `[regression]` issue, closed-as-not-planned mutes. Card actions: File now / Mute / Unmute.
- `server/automations/issue_filing.py`: the shared file/comment/regress decision — #1433 and #1434 migrate the other two filers onto it.
- The daemon now calls `configure_logging()` (Fixes #1423).

Kill switch `CCSWITCH_ERROR_SINK_ENABLED`; the automation ships disabled and is enabled from the Automations page.

Fixes #1423. Refs #1433, #1434.

## Test plan

- [x] `ruff`, `mypy`, `pytest tests/`, `pytest server/tests/` green locally (log: /tmp/error-triage-gate.log)
- [ ] After merge: enable `error-triage` in `file` mode on the primary and watch the first tick file ≤ daily_cap issues

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
gh pr create --repo steiner385/viberpit --base master --title "feat(obs): error triage — capture WARNING+ signatures and file deduplicated backlog issues" --body-file /tmp/error-triage-pr.md
gh pr merge --squash --auto
```

- [ ] **Step 4: Verify the PR is non-draft with auto-merge armed**

```bash
gh api graphql -f query='{repository(owner:"steiner385",name:"viberpit"){pullRequests(states:OPEN,last:1,headRefName:"'"$(git branch --show-current)"'"){nodes{number isDraft autoMergeRequest{enabledAt} mergeStateStatus}}}}'
```

Expected: `isDraft: false`, `autoMergeRequest.enabledAt` set.
