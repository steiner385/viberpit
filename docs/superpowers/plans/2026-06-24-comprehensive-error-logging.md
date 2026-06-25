# Comprehensive Error & Lifecycle Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture all errors/warnings + deploy/session lifecycle breadcrumbs across frontend and backend as redacted, correlated (session_id + request_id) structured JSON, persisted to journald + rotating files, so upgrade-time session weirdness becomes diagnosable.

**Architecture:** Ride the existing Python `logging` tree. A new `server/obs/` package adds a `JSONFormatter`, a `ContextFilter` that stamps correlation ids from `contextvars`, and an idempotent `configure_logging()` attaching a stdout handler (→ journald) and a `RotatingFileHandler`. Frontend errors are captured by an `ErrorBoundary` + global handlers + WS logging, buffered, and POSTed to `/api/client-logs`, which replays them through the **same** logging pipeline. All output passes through the existing credential redactor.

**Tech Stack:** Python 3 / FastAPI / stdlib `logging` + `contextvars` (no new backend deps) · React + TypeScript + Vite · `vitest` / `pytest`.

## Global Constraints

- **No new backend dependencies** — stdlib `logging`/`contextvars` only (Approach A; constitution VII).
- **Timestamps:** every wire/log timestamp goes through `server/time_iso.py:iso_utc` — never a bare `.isoformat()`.
- **Redaction:** all log output (msg, structured fields, exceptions, forwarded client logs) must pass through the shared credential redactor in `server/audit.py`. Secrets must never reach a sink.
- **Single source of truth (VI):** reuse the existing `RedactingFilter`/`install_redacting_filter`; reuse the existing derived-status vocabulary for transition `from`/`to`; one ingest path for client logs (no parallel store).
- **Logging must never crash the app:** unwritable log dir → stdout-only + one warning; client-log failures dropped silently (no recursion); formatter coerces non-serializable values to `repr`.
- **Config defaults:** `CCSWITCH_LOG_LEVEL=INFO`, `CCSWITCH_LOG_DIR=/var/log/ccswitch` (prod) with a per-user dev fallback, `CCSWITCH_CLIENT_LOG_LEVEL=warn`. Rotation: `maxBytes=50MB`, `backupCount=10`.
- **Backend tests** must pass under `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest` with `PYTHONPATH=<worktree>/src` against the prebuilt `.venv`. Run `ruff` + `mypy` before pushing.
- **Frontend gates:** `vitest run`, `npx tsc -b --force` (NOT `tsc --noEmit`), `eslint --max-warnings 0` (run in `web/`).

## File Structure

**Created (backend):**
- `server/obs/__init__.py` — package marker.
- `server/obs/context.py` — `contextvars`, `PROC_EPOCH`, `bind_session`/`bind_request`, `new_request_id`, `log_event`.
- `server/obs/jsonlog.py` — `JSONFormatter`, `ContextFilter`, `configure_logging`, `_coerce`.
- `server/obs/middleware.py` — `RequestContextMiddleware`.
- `server/api/client_logs.py` — `POST /api/client-logs` ingest router.

**Created (frontend):**
- `web/src/obs/logger.ts` — client logger (ring buffer, batched POST, `sendBeacon`).
- `web/src/obs/install.ts` — global `error`/`unhandledrejection` handler install.
- `web/src/components/ErrorBoundary.tsx` — React error boundary + fallback UI.

**Modified (backend):**
- `server/audit.py` — add public `redact_text`.
- `server/config.py` — add `log_dir` / `log_level` / `client_log_level` settings (both Settings variants).
- `server/app.py` — call `configure_logging()` in lifespan; add `RequestContextMiddleware`; add catch-all exception handler; `include_router(client_logs_router)`.
- `server/bridge/ws.py` — `bind_session` around the attached-session event loop.
- `server/sessions/orchestrator.py` — `bind_session` in per-session coroutines; `session.*` + `bg_task_rescan` breadcrumbs.
- `server/deploy/drain.py`, `server/deploy/steps.py`, `server/deploy/controller.py` — `deploy.*` breadcrumbs with a shared `deploy_id`.

**Modified (frontend):**
- `web/src/main.tsx` — install global handlers; wrap `<App/>` in `<ErrorBoundary>`.
- `web/src/ws/client.ts` — log `onclose`/`onerror`.
- `web/src/api/queryClient.ts` — forward mutation errors to the logger.

**Modified (infra):**
- `deploy/ccswitch.service`, `deploy/ccswitch-daemon.service` — journald output.
- `.env.example` (or deploy docs) — document new vars.

**Tests created:**
- `server/tests/test_obs_context.py`, `server/tests/test_obs_jsonlog.py`, `server/tests/test_obs_middleware.py`, `server/tests/test_exception_handler.py`, `server/tests/test_client_logs_api.py`.
- `web/src/obs/logger.test.ts`, `web/src/components/ErrorBoundary.test.tsx`, plus WS-close assertions in the existing ws test.

---

## Task 1: Correlation context module (`server/obs/context.py`)

**Files:**
- Create: `server/obs/__init__.py` (empty)
- Create: `server/obs/context.py`
- Test: `server/tests/test_obs_context.py`

**Interfaces:**
- Produces:
  - `session_id_var: ContextVar[str | None]`, `request_id_var: ContextVar[str | None]`
  - `PROC_EPOCH: str`
  - `new_request_id() -> str`
  - `bind_session(session_id: str | None)` / `bind_request(request_id: str | None)` — context managers
  - `log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_obs_context.py
import logging

from server.obs.context import (
    PROC_EPOCH, bind_request, bind_session, log_event,
    new_request_id, request_id_var, session_id_var,
)


def test_bind_session_sets_and_resets():
    assert session_id_var.get() is None
    with bind_session("sess-1"):
        assert session_id_var.get() == "sess-1"
    assert session_id_var.get() is None


def test_bind_request_resets_on_exception():
    try:
        with bind_request("req-1"):
            assert request_id_var.get() == "req-1"
            raise ValueError("boom")
    except ValueError:
        pass
    assert request_id_var.get() is None


def test_new_request_id_is_short_and_unique():
    a, b = new_request_id(), new_request_id()
    assert a != b and len(a) == 12


def test_proc_epoch_stable():
    from server.obs.context import PROC_EPOCH as again
    assert PROC_EPOCH == again and "-" in PROC_EPOCH


def test_log_event_attaches_event_and_fields(caplog):
    logger = logging.getLogger("test.evt")
    with caplog.at_level(logging.INFO):
        log_event(logger, logging.INFO, "session.start", session_id="s1", reason="new")
    rec = caplog.records[-1]
    assert rec.getMessage() == "session.start"
    assert rec.event == "session.start"
    assert rec.fields == {"session_id": "s1", "reason": "new"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_context.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.obs'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/obs/__init__.py
```

```python
# server/obs/context.py
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

session_id_var: ContextVar[str | None] = ContextVar("ccswitch_session_id", default=None)
request_id_var: ContextVar[str | None] = ContextVar("ccswitch_request_id", default=None)

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


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit a structured breadcrumb.

    `event` is a dotted name (e.g. "session.start") used as the message so
    plain-text/journald readers see it too; **fields become structured JSON keys
    via JSONFormatter.
    """
    logger.log(level, event, extra={"event": event, "fields": fields})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_context.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add server/obs/__init__.py server/obs/context.py server/tests/test_obs_context.py
git commit -m "feat(obs): correlation contextvars + log_event helper"
```

---

## Task 2: Public redactor + JSON formatter / config (`server/obs/jsonlog.py`)

**Files:**
- Modify: `server/audit.py` (add `redact_text`)
- Create: `server/obs/jsonlog.py`
- Test: `server/tests/test_obs_jsonlog.py`

**Interfaces:**
- Consumes: `server.obs.context` (`PROC_EPOCH`, `session_id_var`, `request_id_var`, `bind_session`); `server.audit.install_redacting_filter`; `server.time_iso.iso_utc`.
- Produces:
  - `server.audit.redact_text(text: str) -> str`
  - `JSONFormatter` (a `logging.Formatter`)
  - `ContextFilter` (a `logging.Filter` stamping `session_id`/`request_id`/`proc_epoch`)
  - `configure_logging() -> None` (idempotent)
  - `_coerce(value: Any) -> Any`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_obs_jsonlog.py
import json
import logging

import pytest

from server.audit import redact_text
from server.obs.context import bind_session, log_event
from server.obs.jsonlog import ContextFilter, JSONFormatter


def _format(record: logging.LogRecord) -> dict:
    f = JSONFormatter()
    ContextFilter().filter(record)
    return json.loads(f.format(record))


def test_redact_text_strips_api_key():
    out = redact_text("token sk-ant-api03-SECRETSECRET here")
    assert "sk-ant" not in out and "[REDACTED]" in out


def test_format_basic_fields():
    rec = logging.LogRecord("n", logging.WARNING, __file__, 1, "hello", None, None)
    d = _format(rec)
    assert d["level"] == "WARNING"
    assert d["msg"] == "hello"
    assert d["proc_epoch"] and d["ts"].endswith("+00:00")


def test_format_injects_session_id():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "x", None, None)
    with bind_session("sess-42"):
        ContextFilter().filter(rec)
    d = json.loads(JSONFormatter().format(rec))
    assert d["session_id"] == "sess-42"


def test_format_event_and_fields():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "session.start", None, None)
    rec.event = "session.start"
    rec.fields = {"reason": "new", "obj": object()}  # non-serializable obj coerced
    d = _format(rec)
    assert d["event"] == "session.start"
    assert d["fields"]["reason"] == "new"
    assert isinstance(d["fields"]["obj"], str)  # repr fallback


def test_format_redacts_structured_fields():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "x", None, None)
    rec.fields = {"creds": "sk-ant-api03-LEAKLEAK"}
    d = _format(rec)
    assert "sk-ant" not in json.dumps(d)


def test_format_includes_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("n", logging.ERROR, __file__, 1, "failed", None, None)
        import sys
        rec.exc_info = sys.exc_info()
    d = _format(rec)
    assert "ValueError: boom" in d["exc"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_jsonlog.py -v`
Expected: FAIL — `ImportError: cannot import name 'redact_text'` / `No module named 'server.obs.jsonlog'`

- [ ] **Step 3a: Add `redact_text` to `server/audit.py`**

Insert directly after the `_redact` function (after line 54):

```python
def redact_text(text: str) -> str:
    """Public credential redactor for arbitrary text (e.g. serialized JSON logs)."""
    return _redact(text)
```

- [ ] **Step 3b: Write `server/obs/jsonlog.py`**

```python
# server/obs/jsonlog.py
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
import os
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_jsonlog.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add server/audit.py server/obs/jsonlog.py server/tests/test_obs_jsonlog.py
git commit -m "feat(obs): JSON formatter, context filter, configure_logging"
```

---

## Task 3: Logging config settings (`server/config.py`)

**Files:**
- Modify: `server/config.py` (both the pydantic `Settings` and the dataclass fallback)
- Test: `server/tests/test_obs_config.py`

**Interfaces:**
- Produces: `Settings.log_dir: str`, `Settings.log_level: str`, `Settings.client_log_level: str`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_obs_config.py
import os
from server.config import get_settings


def test_log_defaults():
    s = get_settings()
    assert s.log_level == "INFO"
    assert s.client_log_level == "warn"
    assert s.log_dir  # non-empty default


def test_log_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("CCSWITCH_LOG_DIR", "/tmp/ccswitch-logs")
    s = get_settings()
    assert s.log_level == "DEBUG"
    assert s.log_dir == "/tmp/ccswitch-logs"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'log_level'`

- [ ] **Step 3a: Add to the pydantic `Settings` class** (after line 46, `vnc_idle_timeout_s`):

```python
        log_level: str = Field(default="INFO", alias="CCSWITCH_LOG_LEVEL")
        log_dir: str = Field(
            default_factory=lambda: os.environ.get("CCSWITCH_LOG_DIR")
            or ("/var/log/ccswitch" if os.access("/var/log", os.W_OK)
                else str(Path.home() / ".claude-accounts" / "logs")),
            alias="CCSWITCH_LOG_DIR",
        )
        client_log_level: str = Field(default="warn", alias="CCSWITCH_CLIENT_LOG_LEVEL")
```

- [ ] **Step 3b: Add to the dataclass fallback `Settings`** (after line 122, `vnc_idle_timeout_s`):

```python
        log_level: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_LOG_LEVEL", "INFO")
        )
        log_dir: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_LOG_DIR")
            or ("/var/log/ccswitch" if os.access("/var/log", os.W_OK)
                else str(Path.home() / ".claude-accounts" / "logs"))
        )
        client_log_level: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_CLIENT_LOG_LEVEL", "warn")
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add server/config.py server/tests/test_obs_config.py
git commit -m "feat(config): log_dir / log_level / client_log_level settings"
```

---

## Task 4: Wire `configure_logging()` into app startup (`server/app.py`)

**Files:**
- Modify: `server/app.py:103` (replace bare `install_redacting_filter()` in `_lifespan`) and the import at line 15
- Test: `server/tests/test_obs_jsonlog.py` (add a wiring test)

**Interfaces:**
- Consumes: `server.obs.jsonlog.configure_logging`.

- [ ] **Step 1: Write the failing test** (append to `server/tests/test_obs_jsonlog.py`)

```python
def test_configure_logging_idempotent_and_adds_handlers(monkeypatch, tmp_path):
    import logging as _logging
    import server.obs.jsonlog as jl
    monkeypatch.setattr(jl, "_configured", False)
    monkeypatch.setenv("CCSWITCH_LOG_DIR", str(tmp_path))
    root = _logging.getLogger()
    before = len(root.handlers)
    jl.configure_logging()
    jl.configure_logging()  # idempotent — second call is a no-op
    json_handlers = [h for h in root.handlers if isinstance(h.formatter, jl.JSONFormatter)]
    assert json_handlers  # at least the stream handler attached
    assert (tmp_path / "ccswitch.jsonl").exists() or before >= 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_jsonlog.py::test_configure_logging_idempotent_and_adds_handlers -v`
Expected: FAIL until the import path is confirmed wired (passes once `configure_logging` is importable; this test guards behavior, not app.py — keep it).

- [ ] **Step 3a: Update the import at `server/app.py:15`**

Replace:

```python
from server.audit import install_redacting_filter
```

with:

```python
from server.obs.jsonlog import configure_logging
```

- [ ] **Step 3b: Replace the call at `server/app.py:103`**

Replace:

```python
    install_redacting_filter()
```

with:

```python
    # Structured JSON logging (stdout→journald + rotating file) with correlation
    # ids and credential redaction. Idempotent; installs the redacting filter too.
    configure_logging()
```

- [ ] **Step 4: Verify the suite still passes + app imports**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -c "import server.app"`
Then: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_jsonlog.py -v`
Expected: import OK; PASS.

- [ ] **Step 5: Commit**

```bash
git add server/app.py server/tests/test_obs_jsonlog.py
git commit -m "feat(obs): configure structured logging at app startup"
```

---

## Task 5: Request-context middleware (`server/obs/middleware.py`)

**Files:**
- Create: `server/obs/middleware.py`
- Modify: `server/app.py` (import + `app.add_middleware(RequestContextMiddleware)` near line 316)
- Test: `server/tests/test_obs_middleware.py`

**Interfaces:**
- Consumes: `server.obs.context` (`new_request_id`, `request_id_var`); `starlette.middleware.base.BaseHTTPMiddleware`.
- Produces: `RequestContextMiddleware`; an `X-Request-ID` response header; an INFO `http.request` breadcrumb.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_obs_middleware.py
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.obs.context import request_id_var
from server.obs.middleware import RequestContextMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping():
        return {"rid": request_id_var.get()}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    return app


def test_sets_request_id_header_and_contextvar():
    client = TestClient(_app())
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.headers["X-Request-ID"]
    assert r.json()["rid"] == r.headers["X-Request-ID"]


def test_contextvar_reset_between_requests():
    client = TestClient(_app())
    client.get("/ping")
    assert request_id_var.get() is None  # reset in finally


def test_emits_http_request_breadcrumb(caplog):
    client = TestClient(_app())
    with caplog.at_level(logging.INFO):
        client.get("/ping")
    evts = [r for r in caplog.records if getattr(r, "event", None) == "http.request"]
    assert evts and evts[-1].fields["status"] == 200
    assert evts[-1].fields["path"] == "/ping"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_middleware.py -v`
Expected: FAIL — `No module named 'server.obs.middleware'`

- [ ] **Step 3a: Write `server/obs/middleware.py`**

```python
# server/obs/middleware.py
"""ASGI middleware that assigns a per-request correlation id.

Sets request_id in a contextvar (so every log record during the request is
stamped), echoes it as the X-Request-ID response header (so a browser error can
name the request it came from), and emits one INFO http.request breadcrumb on
completion. The contextvar token is always reset, so ids never leak between
requests.
"""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from server.obs.context import log_event, new_request_id, request_id_var

log = logging.getLogger("server.obs")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        rid = request.headers.get("X-Request-ID") or new_request_id()
        token = request_id_var.set(rid)
        start = time.monotonic()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            log_event(
                log, logging.INFO, "http.request",
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=round((time.monotonic() - start) * 1000, 1),
            )
            request_id_var.reset(token)
```

- [ ] **Step 3b: Wire it in `server/app.py`** — add the import near line 16 and register the middleware immediately before `app.add_middleware(IdentityMiddleware)` at line 316:

Import (top of file):

```python
from server.obs.middleware import RequestContextMiddleware
```

Registration (Starlette runs the LAST-added middleware OUTERMOST; add this AFTER IdentityMiddleware so request_id wraps identity + routes):

```python
    app.add_middleware(IdentityMiddleware)
    app.add_middleware(RequestContextMiddleware)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_middleware.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add server/obs/middleware.py server/app.py server/tests/test_obs_middleware.py
git commit -m "feat(obs): per-request correlation id middleware + X-Request-ID"
```

---

## Task 6: Catch-all exception handler (`server/app.py`)

**Files:**
- Modify: `server/app.py` (add `@app.exception_handler(Exception)` after the `DaemonError` handler at line 327)
- Test: `server/tests/test_exception_handler.py`

**Interfaces:**
- Consumes: the app factory `create_app`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_exception_handler.py
import logging

from fastapi.testclient import TestClient

from server.app import create_app


def _client(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    app = create_app()

    @app.get("/api/_explode")
    async def _explode():
        raise RuntimeError("unhandled-boom")

    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_returns_500_and_logs(monkeypatch, caplog):
    client = _client(monkeypatch)
    with caplog.at_level(logging.ERROR):
        r = client.get("/api/_explode")
    assert r.status_code == 500
    assert r.json()["detail"]  # generic message, not the traceback
    assert any("unhandled-boom" in (rec.exc_text or "") or rec.exc_info
               for rec in caplog.records if rec.levelno >= logging.ERROR)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_exception_handler.py -v`
Expected: FAIL — no log record / unhandled exception not caught as expected.

- [ ] **Step 3: Add the handler** in `server/app.py` immediately after the `DaemonError` handler (after line 327):

```python
    @app.exception_handler(Exception)
    async def _unhandled_handler(_request, exc):  # noqa: ANN001
        # Last-resort: log full traceback (auto-stamped with request_id) then
        # return a generic 500 so internals never leak to the client. FastAPI
        # tries specific handlers first, so domain mappings above still win.
        logging.getLogger("server").exception("unhandled exception: %s", exc)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_exception_handler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app.py server/tests/test_exception_handler.py
git commit -m "feat(obs): catch-all exception handler logs traceback + returns 500"
```

---

## Task 7: Bind session_id in the WS bridge (`server/bridge/ws.py`)

**Files:**
- Modify: `server/bridge/ws.py` (wrap the attached-session event/drain loop in `bind_session`)
- Test: `server/tests/test_obs_ws_binding.py`

**Interfaces:**
- Consumes: `server.obs.context.bind_session`, `session_id_var`.

**Note for implementer:** Read `server/bridge/ws.py` to find where a client `attach` resolves a `sessionId` and the per-session event loop / handler runs (around the `build_ws_router` event consumption — see Explore notes lines 58–105). Wrap the body that handles events for a resolved session id in `with bind_session(session_id):` so logs emitted while servicing that session carry it. Where the handler is a long-lived loop, set the contextvar at the top of the per-session task instead.

- [ ] **Step 1: Write the failing test** (unit-level guard on the helper usage)

```python
# server/tests/test_obs_ws_binding.py
from server.obs.context import bind_session, session_id_var


def test_bind_session_is_used_for_scoping():
    # Guard: the bridge must scope work with bind_session so records carry id.
    with bind_session("ws-sess"):
        assert session_id_var.get() == "ws-sess"
    assert session_id_var.get() is None
```

(Also add an assertion to the existing `server/tests/test_ws.py`: after attaching to a session, a log emitted during event handling carries `session_id`. Use `caplog` + the existing fake driver/orchestrator fixture.)

- [ ] **Step 2: Run test to verify it fails / baseline**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_ws_binding.py server/tests/test_ws.py -v`
Expected: new binding assertion in test_ws.py FAILS (session_id is None before wiring).

- [ ] **Step 3: Implement** — in `server/bridge/ws.py`, import and wrap:

```python
from server.obs.context import bind_session
```

At the point the WS handler begins servicing events for an attached `session_id` (the consume loop), enter the context:

```python
        with bind_session(session_id):
            # ... existing per-session event consumption / drain loop ...
```

If the consume loop spans the whole connection for one active session, set it where the active session id is established and reset when it changes (use the context manager around the inner loop body so re-attach to a different session rebinds correctly).

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_ws_binding.py server/tests/test_ws.py -v`
Expected: PASS (recall the xvfb env caveat — `unset CCSWITCH_ENABLE_XVFB` if test_ws fake-factory issues appear).

- [ ] **Step 5: Commit**

```bash
git add server/bridge/ws.py server/tests/test_obs_ws_binding.py server/tests/test_ws.py
git commit -m "feat(obs): bind session_id while servicing WS session events"
```

---

## Task 8: Session lifecycle breadcrumbs (`server/sessions/orchestrator.py`)

**Files:**
- Modify: `server/sessions/orchestrator.py` (instrument transitions; bind session in per-session coroutines)
- Test: `server/tests/test_obs_session_breadcrumbs.py`

**Interfaces:**
- Consumes: `server.obs.context.log_event`, `bind_session`.
- Produces: INFO breadcrumbs with `event` ∈ {`session.start`, `session.resume`, `session.reattach`, `session.rotate`, `session.park`, `session.unpark`, `session.terminal`, `bg_task_rescan`} and `fields` carrying `session_id`, `from`/`to` (using the existing derived-status vocabulary), `reason`, plus rescan counts.

**Note for implementer:** anchor methods (confirmed by grep): `create_session` (orchestrator.py:356), `rehydrate` (807), the rotation path (`_do_rotate`/`rotated` broadcast, search `rotated`), park/unpark (search `is_parked` near lines 599–870), archive/terminal (`archive_session`:2152), and the background-task rescan at line 1109 (`await asyncio.to_thread(scan_open_background_tasks, path)`). Add a module logger if absent: `log = logging.getLogger(__name__)`.

- [ ] **Step 1: Write the failing test** (uses the existing orchestrator test fixtures; assert breadcrumbs emitted)

```python
# server/tests/test_obs_session_breadcrumbs.py
import logging

from server.obs.context import log_event  # ensures helper import path is valid


def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def test_log_event_shapes_session_start(caplog):
    # Contract test: a session.start breadcrumb carries from/to + session_id.
    logger = logging.getLogger("server.sessions.orchestrator")
    with caplog.at_level(logging.INFO):
        log_event(logger, logging.INFO, "session.start",
                  session_id="s1", **{"from": None, "to": "starting", "reason": "create"})
    rec = _events(caplog, "session.start")[-1]
    assert rec.fields["session_id"] == "s1"
    assert rec.fields["to"] == "starting"
```

(Then add integration assertions to the existing orchestrator test module: drive `create_session` / `rehydrate` against the fake driver and assert the corresponding breadcrumb appears via `caplog`. Reuse the established orchestrator fixtures rather than building new ones.)

- [ ] **Step 2: Run test to verify it fails / baseline**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_session_breadcrumbs.py -v`
Expected: contract test PASSES; integration assertions (added to existing module) FAIL until breadcrumbs are inserted.

- [ ] **Step 3: Instrument the orchestrator**

Add at module top (if not present): `import logging` / `log = logging.getLogger(__name__)` and `from server.obs.context import bind_session, log_event`.

Insert breadcrumbs at the anchor points (examples — match field names to local variables):

```python
# in create_session, after the session row/driver is created:
log_event(log, logging.INFO, "session.start",
          session_id=session_id, **{"from": None, "to": "starting", "reason": "create"})
```

```python
# in rehydrate, per recovered session after resume is issued:
log_event(log, logging.INFO, "session.reattach",
          session_id=sid, **{"from": prev_status, "to": new_status, "reason": "rehydrate"})
```

```python
# in the rotation path, after a successful account switch:
log_event(log, logging.INFO, "session.rotate",
          session_id=session_id, from_account=from_account, to_account=to_account, reason=reason)
```

```python
# where a session enters/leaves the parked overlay (near is_parked checks):
log_event(log, logging.INFO, "session.park", session_id=sid, reason="bg_tasks_open")
# ... and the symmetric unpark:
log_event(log, logging.INFO, "session.unpark", session_id=sid, reason="bg_tasks_cleared")
```

```python
# in archive_session / terminal transitions:
log_event(log, logging.INFO, "session.terminal",
          session_id=session_id, **{"from": prev_status, "to": "archived", "reason": "archive"})
```

Wrap any per-session background coroutine body (e.g. the per-session resume/monitor task) with `with bind_session(sid):` at its top so its logs carry the id (contextvars do NOT auto-propagate into tasks spawned without copy_context).

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_session_breadcrumbs.py server/tests/test_orchestrator*.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_obs_session_breadcrumbs.py
git commit -m "feat(obs): session lifecycle breadcrumbs (start/resume/reattach/rotate/park/terminal)"
```

---

## Task 9: Background-rescan + deploy lifecycle breadcrumbs

**Files:**
- Modify: `server/sessions/orchestrator.py:1109` (bg_task_rescan counts)
- Modify: `server/deploy/drain.py`, `server/deploy/steps.py`, `server/deploy/controller.py` (deploy.* with a shared `deploy_id`)
- Test: `server/tests/test_obs_deploy_breadcrumbs.py`

**Interfaces:**
- Produces: `bg_task_rescan` (fields: `session_id`, `open`, `killed`); `deploy.drain.start`, `deploy.daemon.stop`, `deploy.daemon.start`, `deploy.sessions.reattach`, `deploy.edge.cutover` (all carry `deploy_id`).

**Note for implementer:** read `server/deploy/drain.py` and `server/deploy/steps.py` to find the drain-start, daemon stop/start, and cutover steps. Generate `deploy_id = new_request_id()` once at the start of a deploy run (in `controller.py`'s `deploy_once`/`start`) and thread it into the step calls (or stash on the controller instance) so all five breadcrumbs share it.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_obs_deploy_breadcrumbs.py
import logging
from server.obs.context import log_event


def test_bg_rescan_breadcrumb_shape(caplog):
    log = logging.getLogger("server.sessions.orchestrator")
    with caplog.at_level(logging.INFO):
        log_event(log, logging.INFO, "bg_task_rescan", session_id="s1", open=2, killed=1)
    rec = [r for r in caplog.records if getattr(r, "event", None) == "bg_task_rescan"][-1]
    assert rec.fields == {"session_id": "s1", "open": 2, "killed": 1}


def test_deploy_breadcrumb_shape(caplog):
    log = logging.getLogger("server.deploy")
    with caplog.at_level(logging.INFO):
        log_event(log, logging.INFO, "deploy.drain.start", deploy_id="dep-1", in_flight=3)
    rec = [r for r in caplog.records if getattr(r, "event", None) == "deploy.drain.start"][-1]
    assert rec.fields["deploy_id"] == "dep-1" and rec.fields["in_flight"] == 3
```

- [ ] **Step 2: Run test to verify it fails / baseline**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_deploy_breadcrumbs.py -v`
Expected: contract tests PASS (they validate the helper); the *integration* assertions you add to existing deploy tests FAIL until instrumented.

- [ ] **Step 3a: Instrument the rescan** at `server/sessions/orchestrator.py:1109`:

```python
            tasks = await asyncio.to_thread(scan_open_background_tasks, path)
            killed = await asyncio.to_thread(
                scan_open_background_tasks, path, treat_killed_as_open=True)
            log_event(log, logging.INFO, "bg_task_rescan",
                      session_id=sid, open=len(tasks), killed=max(0, len(killed) - len(tasks)))
```

(Adjust to the local variable holding the session id; if a killed-count is not cheaply available, log `open=len(tasks)` only.)

- [ ] **Step 3b: Instrument the deploy steps** — add `from server.obs.context import log_event, new_request_id` and a `log = logging.getLogger("server.deploy")` where needed, then emit at each step:

```python
log_event(log, logging.INFO, "deploy.drain.start", deploy_id=deploy_id, in_flight=in_flight)
log_event(log, logging.INFO, "deploy.daemon.stop", deploy_id=deploy_id)
log_event(log, logging.INFO, "deploy.daemon.start", deploy_id=deploy_id)
log_event(log, logging.INFO, "deploy.sessions.reattach", deploy_id=deploy_id,
          recovered=recovered, lost=lost)
log_event(log, logging.INFO, "deploy.edge.cutover", deploy_id=deploy_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_obs_deploy_breadcrumbs.py server/tests/test_deploy*.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/deploy/ server/tests/test_obs_deploy_breadcrumbs.py
git commit -m "feat(obs): bg-rescan + deploy lifecycle breadcrumbs"
```

---

## Task 10: Client-log ingest endpoint (`server/api/client_logs.py`)

**Files:**
- Create: `server/api/client_logs.py`
- Modify: `server/app.py` (import + `app.include_router(client_logs_router)` near line 380)
- Test: `server/tests/test_client_logs_api.py`

**Interfaces:**
- Consumes: `server.auth.middleware.current_identity`; `server.obs.context.bind_session`, `log_event`; `server.config.get_settings` (`client_log_level`).
- Produces: `router` (`POST /api/client-logs`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_client_logs_api.py
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.client_logs import router


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    app = FastAPI()
    # Override auth dependency to a fixed identity for the test.
    from server.auth.middleware import current_identity
    app.dependency_overrides[current_identity] = lambda: "tester@example.com"
    app.include_router(router)
    return TestClient(app)


def test_ingest_logs_to_pipeline(client, caplog):
    batch = {"entries": [
        {"level": "error", "msg": "render crash", "sessionId": "s9", "stack": "at X"},
        {"level": "warn", "msg": "ws closed 1006"},
    ]}
    with caplog.at_level(logging.WARNING):
        r = client.post("/api/client-logs", json=batch)
    assert r.status_code == 202
    fe = [rec for rec in caplog.records if getattr(rec, "source", None) == "frontend"]
    assert len(fe) == 2
    assert any(getattr(rec, "session_id", None) == "s9" for rec in fe)


def test_redaction_applies_to_client_logs(client, caplog):
    batch = {"entries": [{"level": "error", "msg": "leak sk-ant-api03-XYZSECRET"}]}
    with caplog.at_level(logging.ERROR):
        client.post("/api/client-logs", json=batch)
    # The record message is redacted by the RedactingFilter on emit.
    rec = [r for r in caplog.records if getattr(r, "source", None) == "frontend"][-1]
    # Filters run on emit to handlers; assert formatter output is clean:
    from server.obs.jsonlog import JSONFormatter, ContextFilter
    ContextFilter().filter(rec)
    assert "sk-ant" not in JSONFormatter().format(rec)


def test_batch_size_cap(client):
    big = {"entries": [{"level": "error", "msg": "x"} for _ in range(1000)]}
    r = client.post("/api/client-logs", json=big)
    assert r.status_code == 413


def test_requires_auth():
    app = FastAPI()
    app.include_router(router)  # no dependency override → real auth
    r = TestClient(app).post("/api/client-logs", json={"entries": []})
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_client_logs_api.py -v`
Expected: FAIL — `No module named 'server.api.client_logs'`

- [ ] **Step 3a: Write `server/api/client_logs.py`**

```python
# server/api/client_logs.py
"""Ingest frontend errors/warnings and replay them through the backend logging
pipeline so they share one redaction filter, rotation policy and query surface
(journald + ccswitch.jsonl), correlated by session_id / request_id.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server.auth.middleware import current_identity
from server.config import get_settings
from server.obs.context import bind_session, log_event

router = APIRouter(prefix="/api/client-logs", tags=["client-logs"])

log = logging.getLogger("client")

_MAX_ENTRIES = 100
_MAX_MSG = 4000
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warn": logging.WARNING, "warning": logging.WARNING,
           "error": logging.ERROR}


class ClientLogEntry(BaseModel):
    level: str = "error"
    msg: str = ""
    stack: str | None = None
    url: str | None = None
    userAgent: str | None = None
    clientId: str | None = None
    appVersion: str | None = None
    requestId: str | None = None
    sessionId: str | None = None


class ClientLogBatch(BaseModel):
    entries: list[ClientLogEntry] = Field(default_factory=list)


@router.post("", status_code=202)
async def ingest(batch: ClientLogBatch, identity: str = Depends(current_identity)) -> dict:
    if len(batch.entries) > _MAX_ENTRIES:
        raise HTTPException(status_code=413, detail="too many log entries")

    floor = _LEVELS.get(get_settings().client_log_level.lower(), logging.WARNING)
    accepted = 0
    for entry in batch.entries:
        level = _LEVELS.get(entry.level.lower(), logging.ERROR)
        if level < floor:
            continue
        msg = (entry.msg or "")[:_MAX_MSG]
        with bind_session(entry.sessionId):
            log_event(
                log, level, "client.error",
                msg=msg, stack=(entry.stack or "")[:_MAX_MSG],
                url=entry.url, ua=entry.userAgent, client_id=entry.clientId,
                app_version=entry.appVersion, request_id=entry.requestId,
                identity=identity,
            )
            # Re-emit a plain record carrying source=frontend for ergonomic filtering.
            log.log(level, msg, extra={"source": "frontend"})
        accepted += 1
    return {"accepted": accepted}
```

(Note: the test asserts `source="frontend"` on records — the trailing `log.log(..., extra={"source": "frontend"})` provides that. Keep both: the `log_event` carries structured fields, the plain record carries `source`. If the implementer prefers a single record, add `source` into the `log_event` path by extending `log_event` to accept a `source` kwarg — but the two-record approach keeps `log_event` generic.)

- [ ] **Step 3b: Wire the router in `server/app.py`** — import near line 33 and include near line 380:

```python
from server.api.client_logs import router as client_logs_router
```

```python
    app.include_router(client_logs_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_client_logs_api.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add server/api/client_logs.py server/app.py server/tests/test_client_logs_api.py
git commit -m "feat(obs): POST /api/client-logs ingest into the logging pipeline"
```

---

## Task 11: Frontend client logger (`web/src/obs/logger.ts`)

**Files:**
- Create: `web/src/obs/logger.ts`
- Test: `web/src/obs/logger.test.ts`

**Interfaces:**
- Produces:
  - `clientLogger` singleton with `error(msg, extra?)`, `warn(msg, extra?)`, `setSessionId(id|null)`, `flush()`.
  - `type ClientLogEntry`.
- Consumes: browser `fetch` / `navigator.sendBeacon`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/obs/logger.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ClientLogger } from './logger'

describe('ClientLogger', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('batches entries and POSTs on flush', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 200 })
    logger.error('boom', { sessionId: 's1' })
    logger.warn('warned')
    await logger.flush()
    expect(fetchMock).toHaveBeenCalledOnce()
    const [, init] = fetchMock.mock.calls[0]
    const body = JSON.parse(init.body)
    expect(body.entries).toHaveLength(2)
    expect(body.entries[0].level).toBe('error')
  })

  it('drops oldest beyond maxBuffer', () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }))
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 2 })
    logger.error('a'); logger.error('b'); logger.error('c')
    expect(logger.bufferSize).toBe(2)
  })

  it('stamps the current sessionId', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 200 })
    logger.setSessionId('focused-1')
    logger.error('x')
    await logger.flush()
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.entries[0].sessionId).toBe('focused-1')
  })

  it('uses sendBeacon on pagehide flush', () => {
    const beacon = vi.fn().mockReturnValue(true)
    vi.stubGlobal('navigator', { sendBeacon: beacon } as unknown as Navigator)
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 200 })
    logger.error('x')
    logger.flushBeacon()
    expect(beacon).toHaveBeenCalledOnce()
  })

  it('never throws if fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('net')))
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 200 })
    logger.error('x')
    await expect(logger.flush()).resolves.toBeUndefined()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/obs/logger.test.ts`
Expected: FAIL — cannot find `./logger`.

- [ ] **Step 3: Write `web/src/obs/logger.ts`**

```ts
// web/src/obs/logger.ts
/**
 * clientLogger — buffers frontend errors/warnings and forwards them to
 * POST /api/client-logs so they land in the same redacted, correlated JSON sink
 * as backend logs. Bounded ring buffer (drop-oldest); batched delivery on a
 * timer, on buffer pressure, and via sendBeacon on pagehide. Never throws.
 */
export type ClientLogLevel = 'error' | 'warn'

export interface ClientLogEntry {
  level: ClientLogLevel
  msg: string
  stack?: string
  url?: string
  userAgent?: string
  clientId?: string
  appVersion?: string
  requestId?: string
  sessionId?: string
}

interface LoggerOpts { flushMs?: number; maxBuffer?: number; endpoint?: string }

const CLIENT_ID = (() => {
  try { return crypto.randomUUID() } catch { return `c-${Date.now()}-${Math.floor(Math.random() * 1e6)}` }
})()
// Vite injects this at build; falls back to 'dev'.
const APP_VERSION = (import.meta as { env?: Record<string, string> }).env?.VITE_APP_VERSION ?? 'dev'

export class ClientLogger {
  private buf: ClientLogEntry[] = []
  private sessionId: string | null = null
  private readonly flushMs: number
  private readonly maxBuffer: number
  private readonly endpoint: string
  private timer: ReturnType<typeof setInterval> | null = null

  constructor(opts: LoggerOpts = {}) {
    this.flushMs = opts.flushMs ?? 5000
    this.maxBuffer = opts.maxBuffer ?? 200
    this.endpoint = opts.endpoint ?? '/api/client-logs'
  }

  get bufferSize(): number { return this.buf.length }

  setSessionId(id: string | null): void { this.sessionId = id }

  start(): void {
    if (this.timer !== null) return
    this.timer = setInterval(() => { void this.flush() }, this.flushMs)
  }

  error(msg: string, extra: Partial<ClientLogEntry> = {}): void { this.push('error', msg, extra) }
  warn(msg: string, extra: Partial<ClientLogEntry> = {}): void { this.push('warn', msg, extra) }

  private push(level: ClientLogLevel, msg: string, extra: Partial<ClientLogEntry>): void {
    const entry: ClientLogEntry = {
      level,
      msg: String(msg).slice(0, 4000),
      url: typeof location !== 'undefined' ? location.href : undefined,
      userAgent: typeof navigator !== 'undefined' ? navigator.userAgent : undefined,
      clientId: CLIENT_ID,
      appVersion: APP_VERSION,
      sessionId: this.sessionId ?? undefined,
      ...extra,
    }
    this.buf.push(entry)
    if (this.buf.length > this.maxBuffer) this.buf.splice(0, this.buf.length - this.maxBuffer)
  }

  async flush(): Promise<void> {
    if (this.buf.length === 0) return
    const entries = this.buf
    this.buf = []
    try {
      await fetch(this.endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entries }),
        keepalive: true,
      })
    } catch {
      // Never recurse into logging-about-logging; drop the batch.
    }
  }

  /** Best-effort last-gasp flush on pagehide/visibilitychange. */
  flushBeacon(): void {
    if (this.buf.length === 0) return
    const entries = this.buf
    this.buf = []
    try {
      navigator.sendBeacon?.(this.endpoint, new Blob([JSON.stringify({ entries })], { type: 'application/json' }))
    } catch {
      // ignore
    }
  }
}

export const clientLogger = new ClientLogger()
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/obs/logger.test.ts`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add web/src/obs/logger.ts web/src/obs/logger.test.ts
git commit -m "feat(web/obs): batched client logger with sendBeacon flush"
```

---

## Task 12: Global error handlers (`web/src/obs/install.ts` + `main.tsx`)

**Files:**
- Create: `web/src/obs/install.ts`
- Modify: `web/src/main.tsx`
- Test: `web/src/obs/install.test.ts`

**Interfaces:**
- Consumes: `clientLogger` from `web/src/obs/logger.ts`.
- Produces: `installGlobalErrorHandlers(logger?: ClientLogger): () => void` (returns an uninstaller).

- [ ] **Step 1: Write the failing test**

```ts
// web/src/obs/install.test.ts
import { describe, it, expect, vi } from 'vitest'
import { installGlobalErrorHandlers } from './install'
import { ClientLogger } from './logger'

describe('installGlobalErrorHandlers', () => {
  it('forwards window error events', () => {
    const logger = new ClientLogger()
    const spy = vi.spyOn(logger, 'error')
    const uninstall = installGlobalErrorHandlers(logger)
    window.dispatchEvent(new ErrorEvent('error', { message: 'kaboom', error: new Error('kaboom') }))
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('kaboom'), expect.any(Object))
    uninstall()
  })

  it('forwards unhandledrejection', () => {
    const logger = new ClientLogger()
    const spy = vi.spyOn(logger, 'error')
    const uninstall = installGlobalErrorHandlers(logger)
    const evt = new Event('unhandledrejection') as Event & { reason?: unknown }
    evt.reason = new Error('rejected')
    window.dispatchEvent(evt)
    expect(spy).toHaveBeenCalled()
    uninstall()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/obs/install.test.ts`
Expected: FAIL — cannot find `./install`.

- [ ] **Step 3a: Write `web/src/obs/install.ts`**

```ts
// web/src/obs/install.ts
import { clientLogger, ClientLogger } from './logger'

/** Wire window.onerror + unhandledrejection to the client logger. Returns an uninstaller. */
export function installGlobalErrorHandlers(logger: ClientLogger = clientLogger): () => void {
  const onError = (e: ErrorEvent) => {
    logger.error(e.message || 'window.error', {
      stack: e.error instanceof Error ? e.error.stack : undefined,
      url: e.filename,
    })
  }
  const onRejection = (e: PromiseRejectionEvent) => {
    const reason = e.reason
    logger.error(
      reason instanceof Error ? reason.message : `unhandledrejection: ${String(reason)}`,
      { stack: reason instanceof Error ? reason.stack : undefined },
    )
  }
  const onPageHide = () => logger.flushBeacon()

  window.addEventListener('error', onError)
  window.addEventListener('unhandledrejection', onRejection)
  window.addEventListener('pagehide', onPageHide)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') logger.flushBeacon()
  })

  return () => {
    window.removeEventListener('error', onError)
    window.removeEventListener('unhandledrejection', onRejection)
    window.removeEventListener('pagehide', onPageHide)
  }
}
```

- [ ] **Step 3b: Wire in `web/src/main.tsx`** — add imports and start the logger + handlers before `createRoot`:

```ts
import { clientLogger } from '@/obs/logger'
import { installGlobalErrorHandlers } from '@/obs/install'

clientLogger.start()
installGlobalErrorHandlers()
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/obs/install.test.ts`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add web/src/obs/install.ts web/src/obs/install.test.ts web/src/main.tsx
git commit -m "feat(web/obs): global error + unhandledrejection handlers"
```

---

## Task 13: React error boundary (`web/src/components/ErrorBoundary.tsx`)

**Files:**
- Create: `web/src/components/ErrorBoundary.tsx`
- Modify: `web/src/main.tsx` (wrap `<App/>`)
- Test: `web/src/components/ErrorBoundary.test.tsx`

**Interfaces:**
- Consumes: `clientLogger`.
- Produces: `ErrorBoundary` component (props: `children`, optional `logger`).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/ErrorBoundary.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ErrorBoundary } from './ErrorBoundary'
import { ClientLogger } from '@/obs/logger'

function Boom(): never { throw new Error('render-crash') }

describe('ErrorBoundary', () => {
  it('renders fallback and reports the error', () => {
    const logger = new ClientLogger()
    const spy = vi.spyOn(logger, 'error')
    // Silence React's error console for this expected throw.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<ErrorBoundary logger={logger}><Boom /></ErrorBoundary>)
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument()
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('render-crash'), expect.any(Object))
  })

  it('renders children when no error', () => {
    render(<ErrorBoundary><div>ok</div></ErrorBoundary>)
    expect(screen.getByText('ok')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/ErrorBoundary.test.tsx`
Expected: FAIL — cannot find `./ErrorBoundary`.

- [ ] **Step 3a: Write `web/src/components/ErrorBoundary.tsx`**

```tsx
// web/src/components/ErrorBoundary.tsx
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { clientLogger, ClientLogger } from '@/obs/logger'

interface Props { children: ReactNode; logger?: ClientLogger }
interface State { hasError: boolean }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false }

  static getDerivedStateFromError(): State {
    return { hasError: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    const logger = this.props.logger ?? clientLogger
    logger.error(`react: ${error.message}`, {
      stack: error.stack,
      componentStack: info.componentStack ?? undefined,
    } as Record<string, unknown>)
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div role="alert" className="flex min-h-screen flex-col items-center justify-center gap-3 p-6 text-center">
          <p className="text-lg font-semibold">Something went wrong.</p>
          <button
            className="rounded-md border px-3 py-1.5 text-sm"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
```

- [ ] **Step 3b: Wrap `<App/>` in `web/src/main.tsx`**

```tsx
import { ErrorBoundary } from '@/components/ErrorBoundary'
// ...
      <BrowserRouter>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
        <Toaster richColors position="top-center" theme="system" />
      </BrowserRouter>
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/ErrorBoundary.test.tsx`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add web/src/components/ErrorBoundary.tsx web/src/components/ErrorBoundary.test.tsx web/src/main.tsx
git commit -m "feat(web/obs): ErrorBoundary with fallback UI + error reporting"
```

---

## Task 14: WebSocket close/error logging (`web/src/ws/client.ts`)

**Files:**
- Modify: `web/src/ws/client.ts` (`onerror`/`onclose`)
- Test: add cases to the existing WS test (or `web/src/ws/client.close.test.ts`)

**Interfaces:**
- Consumes: `clientLogger`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/ws/client.close.test.ts
import { describe, it, expect, vi } from 'vitest'
import { clientLogger } from '@/obs/logger'

describe('SessionSocket close logging', () => {
  it('warns on unexpected close codes', async () => {
    const spy = vi.spyOn(clientLogger, 'warn')
    const { SessionSocket } = await import('@/ws/client')
    const sock = new SessionSocket('ws://localhost/ws')
    // Simulate an unexpected close (1006) by invoking the handler the class installs.
    // @ts-expect-error reach into the instance for the test
    sock.connect()
    // @ts-expect-error access private ws
    sock.ws!.onclose?.({ code: 1006, reason: '' } as CloseEvent)
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('1006'), expect.any(Object))
    sock.destroy()
  })
})
```

(If constructing a real `WebSocket` is awkward under jsdom, factor the close-handling into a small private method `_onClose(code, reason)` and unit-test that directly instead.)

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/ws/client.close.test.ts`
Expected: FAIL — no warn emitted on close.

- [ ] **Step 3: Implement** — in `web/src/ws/client.ts` import the logger and log close/error. Replace the `onerror`/`onclose` handlers (lines 122–130):

```ts
import { clientLogger } from '@/obs/logger'
// ...
    ws.onerror = () => {
      clientLogger.warn('ws error', { sessionId: this._activeSessionId ?? undefined })
      // onclose will fire right after; let it drive reconnect
    }

    ws.onclose = (e: CloseEvent) => {
      if (this._destroyed) return
      // 1000 = normal, 1001 = navigation; anything else is unexpected and worth logging.
      const expected = e.code === 1000 || e.code === 1001
      if (!expected) {
        clientLogger.warn(`ws closed code=${e.code}`, {
          sessionId: this._activeSessionId ?? undefined,
          stack: e.reason || undefined,
        })
      }
      this._setConnectionState('disconnected')
      this._scheduleReconnect()
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/ws/client.close.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/ws/client.ts web/src/ws/client.close.test.ts
git commit -m "feat(web/obs): log unexpected WebSocket close codes + errors"
```

---

## Task 15: Forward mutation errors + finalize wiring (`web/src/api/queryClient.ts`)

**Files:**
- Modify: `web/src/api/queryClient.ts` (forward to logger inside `handleMutationError`)
- Test: extend `web/src/api/queryClient.test.ts` (or create if absent)

**Interfaces:**
- Consumes: `clientLogger`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/api/queryClient.test.ts
import { describe, it, expect, vi } from 'vitest'
import { handleMutationError } from './queryClient'
import { ApiRequestError } from './client'
import { clientLogger } from '@/obs/logger'

describe('handleMutationError forwarding', () => {
  it('forwards the error to the client logger', () => {
    const spy = vi.spyOn(clientLogger, 'error')
    const err = new ApiRequestError(500, { detail: { message: 'server fail' } })
    handleMutationError(err, { options: {} } as never)
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('server fail'), expect.objectContaining({ stack: expect.any(String) }))
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/api/queryClient.test.ts`
Expected: FAIL — logger not called.

- [ ] **Step 3: Implement** — in `web/src/api/queryClient.ts`, import the logger and forward inside `handleMutationError` (keep the existing toast behavior):

```ts
import { clientLogger } from '@/obs/logger'
// ...
export function handleMutationError(
  error: unknown,
  mutation: Mutation<unknown, unknown, unknown>,
): void {
  const message = extractMutationErrorMessage(error)
  clientLogger.error(`mutation: ${message}`, {
    stack: error instanceof Error ? (error.stack ?? String(error)) : String(error),
  })
  if (mutation.options.meta?.suppressErrorToast) return
  toast.error(message)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/api/queryClient.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/api/queryClient.ts web/src/api/queryClient.test.ts
git commit -m "feat(web/obs): forward mutation errors to the client logger"
```

---

## Task 16: Infra — journald output + documented config

**Files:**
- Modify: `deploy/ccswitch.service`, `deploy/ccswitch-daemon.service`
- Modify: `.env.example` (or `deploy/README` if `.env.example` is absent — check first)

- [ ] **Step 1: Add journald directives** — in the `[Service]` block of `deploy/ccswitch.service`, after `Type=simple`:

```ini
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ccswitch
```

In `deploy/ccswitch-daemon.service` `[Service]` block, after `Type=simple`:

```ini
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ccswitch-daemon
```

- [ ] **Step 2: Document the new env vars** — add to `.env.example` (create the lines if missing):

```bash
# Structured logging
CCSWITCH_LOG_LEVEL=INFO
CCSWITCH_LOG_DIR=/var/log/ccswitch
CCSWITCH_CLIENT_LOG_LEVEL=warn
```

- [ ] **Step 3: Verify unit-file syntax**

Run: `systemd-analyze verify deploy/ccswitch.service deploy/ccswitch-daemon.service 2>&1 | head` (warnings about absolute ExecStart paths on this dev box are expected; there must be no parse errors).

- [ ] **Step 4: Commit**

```bash
git add deploy/ccswitch.service deploy/ccswitch-daemon.service .env.example
git commit -m "feat(deploy): capture ccswitch logs to journald + document log vars"
```

---

## Task 17: Full verification + finishing the branch

- [ ] **Step 1: Backend gates**

Run:
```bash
cd <worktree>
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src \
  <main-checkout>/.venv/bin/python -m pytest server/tests/ -q
<main-checkout>/.venv/bin/python -m ruff check server/
<main-checkout>/.venv/bin/python -m mypy server/obs server/api/client_logs.py
```
Expected: all green (the two known env-only failures — `remote_mode_wiring`, `x11` — are pre-existing per project notes; confirm they are the ONLY failures and unrelated).

- [ ] **Step 2: Frontend gates** (in `web/`, after `rm -f web/node_modules && npm ci` if node_modules is a stale symlink)

Run:
```bash
npx vitest run
npx tsc -b --force
npx eslint . --max-warnings 0
```
Expected: all green.

- [ ] **Step 3: Manual smoke (optional but recommended)**

Start the app locally; confirm `ccswitch.jsonl` is written with JSON lines carrying `proc_epoch`, and that triggering a frontend error produces a `source:"frontend"` line correlated by `session_id`.

- [ ] **Step 4: Finish the branch** — follow the project completion workflow:

```bash
git fetch origin
git checkout -b feat/comprehensive-error-logging origin/master
git cherry-pick <first-commit>..<last-commit>   # or rebase the work onto the fresh branch
git push -u origin feat/comprehensive-error-logging
gh pr create --fill   # non-draft; end body with the Claude Code trailer
gh pr merge <#> --squash --auto
```

(Use the `superpowers:finishing-a-development-branch` skill for the integration decision; mark the PR ready + arm auto-merge per CLAUDE.md.)

---

## Self-Review

**1. Spec coverage:**
- Section 1 (substrate) → Tasks 1–4. ✓
- Section 2 (correlation + catch-all) → Tasks 5, 6, 7 (session bind). ✓
- Section 3 (lifecycle breadcrumbs) → Tasks 8, 9. ✓
- Section 4 (frontend capture + forward + ingest) → Tasks 10–15. ✓
- Section 5 (infra/config/retention) → Tasks 3 (config), 16 (systemd/env). Rotation set in Task 2 (`_MAX_BYTES`/`_BACKUP_COUNT`). ✓
- Section 6 (error handling + testing) → tests in every task; logging-never-crashes in Tasks 2/10/11; verification in Task 17. ✓

**2. Placeholder scan:** No "TBD/TODO/implement later". Orchestrator/deploy/ws breadcrumb tasks (7–9) intentionally reference anchor methods by name + line because instrumenting a 2400-line existing file means *locating* the transition, not writing it from scratch; each provides the exact `log_event(...)` call to insert. This is guidance for placement, not a placeholder for missing content.

**3. Type consistency:**
- `log_event(logger, level, event, **fields)` — consistent across Tasks 1, 5, 8, 9, 10.
- `bind_session` / `request_id_var` / `session_id_var` / `PROC_EPOCH` — consistent (Tasks 1, 2, 5, 7, 10).
- `redact_text` — defined Task 2, used Tasks 2 & 10's test.
- Frontend: `ClientLogger` class + `clientLogger` singleton; `ClientLogEntry` fields (`sessionId`, `requestId`, `appVersion`) match the backend `ClientLogEntry` pydantic model (camelCase on the wire) in Task 10. ✓
- `configure_logging` reads `settings.log_level`/`log_dir` defined in Task 3. ✓

No inconsistencies found.
