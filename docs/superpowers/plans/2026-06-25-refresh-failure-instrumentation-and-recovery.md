# Refresh-failure Instrumentation & Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a mass `needs-reauth` event provable and recoverable — capture the refresh-failure body in a durable audit event, stop the non-idempotent refresh retry from orphaning a single-use token, and recover from the live cfg dir before flipping an account.

**Architecture:** The core (`src/ccswitch`) stays DB-free: it raises a structured `AuthError` carrying the HTTP status + truncated body, and emits an `AuthFailure` to an injectable sink at each `needs-reauth` flip. The server registers a thread-safe-queue sink at startup and an async drainer writes the `account.needs_reauth` audit event through the existing redaction layer. A second flip site (`orchestrator._mark_needs_reauth`) writes the same event directly.

**Tech Stack:** Python 3.12, stdlib `urllib`, SQLAlchemy async (`server/db`), FastAPI lifespan, pytest.

## Global Constraints

- **Run backend tests against the prebuilt env** (the worktree has no built venv):
  `PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/b66fd6b7/src \
   /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <args>` — the
  `PYTHONPATH` override is REQUIRED so the worktree's `src/ccswitch` is imported, not the venv's
  stale editable deploy checkout.
- **Core (`src/ccswitch`) must never import `server.*` or any DB** — it stays runnable from the CLI.
- **New audit detail must never contain token material** — redaction is enforced at
  `server/audit.py:record_audit`; tests assert zero `accessToken`/`refreshToken` leakage.
- **`add new model columns nullable`** — N/A here (no schema change; `audit_events` already exists).
- Lint gates run in CI and locally: `ruff`, `mypy` (skips untyped bodies), `pytest`. Run all three.
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Structured `AuthError` carrying status + body

**Files:**
- Modify: `src/ccswitch/oauth.py` (`AuthError` class ~line 39; `refresh_access_token` ~line 240-244)
- Test: `tests/test_oauth.py`

**Interfaces:**
- Produces: `AuthError(message: str, *, status: int | None = None, body: str | None = None)` with
  attributes `.status` and `.body`. `refresh_access_token` raises it with `status` set and `body`
  set to the response body truncated to 2048 chars (decoded latin-1, never raising on bytes).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth.py  (add inside the existing refresh test class)
def test_refresh_400_autherror_carries_status_and_body(self):
    from unittest import mock
    import pytest
    from ccswitch import oauth
    body = b'{"error":"invalid_grant","error_description":"expired"}'
    with mock.patch.object(oauth, "http_request", return_value=(400, body)):
        with pytest.raises(oauth.AuthError) as ei:
            oauth.refresh_access_token("oldR", {})
    assert ei.value.status == 400
    assert "invalid_grant" in ei.value.body

def test_refresh_400_body_is_truncated(self):
    from unittest import mock
    import pytest
    from ccswitch import oauth
    big = b'{"error":"' + b"x" * 5000 + b'"}'
    with mock.patch.object(oauth, "http_request", return_value=(400, big)):
        with pytest.raises(oauth.AuthError) as ei:
            oauth.refresh_access_token("oldR", {})
    assert len(ei.value.body) <= 2048
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest tests/test_oauth.py -k "carries_status_and_body or body_is_truncated" -v`
Expected: FAIL — `AttributeError: 'AuthError' object has no attribute 'status'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/ccswitch/oauth.py — replace the AuthError class body
class AuthError(Exception):
    """Token genuinely rejected (400 invalid_grant / 401) — needs refresh or re-auth.

    Carries the HTTP status and a truncated response body so the failure can be
    audited and (later) classified against the real error code instead of guessed.
    """
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _truncate_body(body: bytes, limit: int = 2048) -> str:
    """Decode a response body to text, lossless-ish, truncated to `limit` chars."""
    return body[:limit].decode("latin-1", errors="replace")
```

```python
# src/ccswitch/oauth.py — in refresh_access_token, replace the 400/401 branch
    if status in (400, 401):
        # The ONLY statuses that prove the grant itself is rejected (400
        # invalid_grant / 401 unauthorized). Carry the body so the needs-reauth
        # flip is auditable and a future classifier can read the real error code.
        raise AuthError(
            f"token refresh failed with {status}",
            status=status, body=_truncate_body(body),
        )
```

- [ ] **Step 4: Run tests to verify they pass (and nothing regressed)**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest tests/test_oauth.py -v`
Expected: PASS, including the pre-existing `test_refresh_400_invalid_grant_raises_autherror`.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/oauth.py tests/test_oauth.py
git commit -m "feat(oauth): carry status+body on AuthError for auditable refresh failures

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Non-idempotent retry policy for the token POST

**Files:**
- Modify: `src/ccswitch/oauth.py` (`http_request` ~line 119-166; `refresh_access_token` call ~line 229)
- Test: `tests/test_oauth.py`

**Interfaces:**
- Consumes: `AuthError` from Task 1 (unchanged here).
- Produces: `http_request(..., idempotent: bool = True)`. When `idempotent=False`: retries ONLY
  edge-reject statuses `{403, 429}` (request never reached the origin); does NOT retry `5xx`
  (origin may have processed and rotated the single-use token); raises `TransientError`
  immediately on a network error/timeout without resending. `refresh_access_token` calls
  `http_request(..., idempotent=False)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth.py
def test_http_request_non_idempotent_does_not_retry_5xx(self):
    from unittest import mock
    from ccswitch import oauth
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise oauth.urllib.error.HTTPError("https://x", 503, "err", {}, __import__("io").BytesIO(b"down"))
    with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
        status, body = oauth.http_request("POST", "https://x", data=b"f", idempotent=False,
                                          max_retries=3, sleep=lambda *_: None)
    assert status == 503
    assert calls["n"] == 1   # NOT resent — would risk consuming a rotated token

def test_http_request_non_idempotent_still_retries_429(self):
    from unittest import mock
    from ccswitch import oauth
    seq = [(429, b"slow"), (200, b"{}")]
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        i = calls["n"]; calls["n"] += 1
        st, bd = seq[min(i, len(seq) - 1)]
        if st != 200:
            raise oauth.urllib.error.HTTPError("https://x", st, "err", {}, __import__("io").BytesIO(bd))
        class R:
            status = 200
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
    with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
        status, _ = oauth.http_request("POST", "https://x", data=b"f", idempotent=False,
                                       max_retries=3, sleep=lambda *_: None)
    assert status == 200
    assert calls["n"] == 2   # 429 is an edge reject → safe to retry

def test_http_request_non_idempotent_network_error_raises_without_retry(self):
    from unittest import mock
    import pytest
    from ccswitch import oauth
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise TimeoutError("read timed out")
    with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(oauth.TransientError):
            oauth.http_request("POST", "https://x", data=b"f", idempotent=False,
                               max_retries=3, sleep=lambda *_: None)
    assert calls["n"] == 1

def test_refresh_passes_idempotent_false(self):
    from unittest import mock
    from ccswitch import oauth
    with mock.patch.object(oauth, "http_request", return_value=(200, b'{"access_token":"a","expires_in":1}')) as hr:
        oauth.refresh_access_token("oldR", {})
    assert hr.call_args.kwargs.get("idempotent") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest tests/test_oauth.py -k "non_idempotent or idempotent_false" -v`
Expected: FAIL — `http_request() got an unexpected keyword argument 'idempotent'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/ccswitch/oauth.py — http_request signature: add idempotent param
def http_request(method: str, url: str, headers: dict | None = None,
                 data: bytes | None = None, timeout: int = 15, *,
                 max_retries: int = _DEFAULT_MAX_RETRIES,
                 max_total_sec: float = _DEFAULT_MAX_TOTAL_SEC,
                 idempotent: bool = True,
                 sleep=time.sleep, rng=random.random, clock=time.monotonic) -> tuple[int, bytes]:
```

```python
# src/ccswitch/oauth.py — replace the retry loop body (the for-loop at ~line 152)
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, method=method, headers=hdrs, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
            # Non-idempotent (single-use token POST): only retry edge rejects that
            # never reached the origin (403 Cloudflare 1010 / 429). A 5xx may mean
            # the origin already processed + rotated the token, so resending would
            # consume the rotation we never captured — do not retry it.
            retryable = (status in (403, 429)) if not idempotent else _is_transient_status(status)
            if attempt < max_retries and retryable and _retry_sleep(e.headers, attempt):
                continue
            return status, body
        except (urllib.error.URLError, TimeoutError) as e:
            # A network failure on a non-idempotent send is ambiguous (the request
            # may have been processed). Never resend — surface as transient so the
            # caller leaves account status unchanged, without consuming the token twice.
            if idempotent and attempt < max_retries and _retry_sleep(None, attempt):
                continue
            raise TransientError(
                f"network error after {attempt + 1} attempt(s): {e}") from e
```

```python
# src/ccswitch/oauth.py — in refresh_access_token, pass idempotent=False to the POST
    status, body = http_request(
        "POST", TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form.encode("utf-8"),
        max_retries=max_retries, max_total_sec=max_total_sec, idempotent=False,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest tests/test_oauth.py -v`
Expected: PASS — all new tests plus the existing 429/5xx/403/400 refresh tests (those mock
`http_request` directly so they are unaffected by the retry-policy change).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/oauth.py tests/test_oauth.py
git commit -m "fix(oauth): don't resend the single-use refresh token after ambiguous failures

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Core auth-failure sink seam (`AuthFailure` + emit at the flip)

**Files:**
- Modify: `src/ccswitch/accounts.py` (imports/top-of-module; `usage_for` AuthError handler ~line 147-151)
- Test: `tests/test_accounts.py`

**Interfaces:**
- Consumes: `oauth.AuthError` with `.status`/`.body` (Task 1).
- Produces:
  - `@dataclass class AuthFailure: account: str; status: int | None; body: str | None; path: str; token_expires_at: int | None; attempts: int`
  - `set_auth_failure_sink(fn: Callable[[AuthFailure], None] | None) -> None` (module-global setter)
  - `emit_auth_failure(f: AuthFailure) -> None` — calls the sink if set, swallowing any exception.
  - `usage_for(..., path: str = "unknown")` — new keyword; on the needs-reauth flip it builds an
    `AuthFailure(path=path, ...)` and calls `emit_auth_failure` before returning.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_accounts.py
def test_usage_for_emits_auth_failure_on_dead_token(monkeypatch, tmp_path):
    from ccswitch import accounts, oauth
    captured = []
    accounts.set_auth_failure_sink(captured.append)
    try:
        # Force the refresh path to raise a dead-grant AuthError with a body.
        def boom(name):
            raise oauth.AuthError("token refresh failed with 400", status=400,
                                  body='{"error":"invalid_grant"}')
        monkeypatch.setattr(accounts, "ensure_valid_token", boom)
        # No live cfg dir for recovery → flip proceeds (Task 4 adds recovery; here it
        # is absent so the flip path runs).
        accounts.usage_for("primary", path="poller-idle")
    finally:
        accounts.set_auth_failure_sink(None)
    assert len(captured) == 1
    f = captured[0]
    assert f.account == "primary" and f.status == 400 and f.path == "poller-idle"
    assert "invalid_grant" in f.body

def test_set_sink_none_is_noop(monkeypatch):
    from ccswitch import accounts, oauth
    accounts.set_auth_failure_sink(None)  # unset
    # emit must not raise even with no sink:
    accounts.emit_auth_failure(accounts.AuthFailure(
        account="x", status=400, body="b", path="cli", token_expires_at=None, attempts=1))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest tests/test_accounts.py -k "emits_auth_failure or set_sink_none" -v`
Expected: FAIL — `AttributeError: module 'ccswitch.accounts' has no attribute 'set_auth_failure_sink'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/ccswitch/accounts.py — near the top imports
from dataclasses import dataclass
from typing import Callable


@dataclass
class AuthFailure:
    account: str
    status: int | None
    body: str | None
    path: str
    token_expires_at: int | None
    attempts: int


_auth_failure_sink: Callable[[AuthFailure], None] | None = None


def set_auth_failure_sink(fn: "Callable[[AuthFailure], None] | None") -> None:
    """Register (or clear) the sink that receives an AuthFailure at each needs-reauth flip.

    The core stays DB-free: the server registers a queue-backed sink at startup. When
    unset (CLI usage) emission is a no-op beyond the existing log line.
    """
    global _auth_failure_sink
    _auth_failure_sink = fn


def emit_auth_failure(f: AuthFailure) -> None:
    sink = _auth_failure_sink
    if sink is None:
        return
    try:
        sink(f)
    except Exception:  # never let instrumentation break the auth path
        log.warning("emit_auth_failure: sink raised", exc_info=True)
```

```python
# src/ccswitch/accounts.py — usage_for signature: thread a `path` label
def usage_for(name: str, *, allow_refresh: bool = True, path: str = "unknown") -> oauth.Usage | None:
```

```python
# src/ccswitch/accounts.py — in usage_for, replace the AuthError handler (~line 147-151)
    except oauth.AuthError as e:
        log.warning("usage_for(%s): refresh token dead (%s) — marking needs-reauth", name, e)
        emit_auth_failure(AuthFailure(
            account=name, status=getattr(e, "status", None), body=getattr(e, "body", None),
            path=path, token_expires_at=_slot_expires_at(name), attempts=1))
        with ledger.locked_ledger() as led:
            ledger.set_status(led, name, "needs-reauth")
        return None
```

```python
# src/ccswitch/accounts.py — small helper (place near _is_fresh)
def _slot_expires_at(name: str) -> int | None:
    try:
        return read_credentials(paths.slot_path(name)).get("expiresAt")
    except Exception:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest tests/test_accounts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/accounts.py tests/test_accounts.py
git commit -m "feat(accounts): AuthFailure sink seam emitted at the needs-reauth flip

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Recoverable `acquire_valid_token` (sync_back fallback before flipping)

**Files:**
- Modify: `src/ccswitch/accounts.py` (new `acquire_valid_token`; route `usage_for` Step-1 through it)
- Test: `tests/test_accounts.py`

**Interfaces:**
- Consumes: `ensure_valid_token`, `sync_back`, `_is_fresh`, `read_credentials`, `paths.slot_path`.
- Produces: `acquire_valid_token(name: str) -> dict` — returns a fresh token blob. On an
  `AuthError` from `ensure_valid_token`, runs `sync_back(name)` and, if the slot now holds a
  fresh token (the live SDK rotated one the poller had not captured), returns it; otherwise
  re-raises the original `AuthError`. `usage_for`'s `allow_refresh=True` path calls
  `acquire_valid_token` instead of `ensure_valid_token`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_accounts.py
def test_acquire_valid_token_recovers_via_sync_back(monkeypatch):
    from ccswitch import accounts, oauth
    state = {"synced": False}
    def boom(name):
        raise oauth.AuthError("token refresh failed with 400", status=400, body="{}")
    def fake_sync_back(name):
        state["synced"] = True
    fresh = {"accessToken": "new", "refreshToken": "newR", "expiresAt": 9_999_999_999_000}
    def fake_read(path):
        return fresh if state["synced"] else {"accessToken": "old", "expiresAt": 0}
    monkeypatch.setattr(accounts, "ensure_valid_token", boom)
    monkeypatch.setattr(accounts, "sync_back", fake_sync_back)
    monkeypatch.setattr(accounts, "read_credentials", fake_read)
    monkeypatch.setattr(accounts, "_is_fresh", lambda blob, cfg: blob.get("expiresAt", 0) > 1)
    out = accounts.acquire_valid_token("primary")
    assert out["accessToken"] == "new" and state["synced"] is True

def test_acquire_valid_token_reraises_when_no_fresher_copy(monkeypatch):
    from ccswitch import accounts, oauth
    import pytest
    def boom(name):
        raise oauth.AuthError("token refresh failed with 400", status=400, body="{}")
    monkeypatch.setattr(accounts, "ensure_valid_token", boom)
    monkeypatch.setattr(accounts, "sync_back", lambda name: None)
    monkeypatch.setattr(accounts, "read_credentials", lambda path: {"accessToken": "old", "expiresAt": 0})
    monkeypatch.setattr(accounts, "_is_fresh", lambda blob, cfg: False)
    with pytest.raises(oauth.AuthError):
        accounts.acquire_valid_token("primary")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest tests/test_accounts.py -k acquire_valid_token -v`
Expected: FAIL — `AttributeError: module 'ccswitch.accounts' has no attribute 'acquire_valid_token'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/ccswitch/accounts.py — new function (place just after ensure_valid_token)
def acquire_valid_token(name: str) -> dict:
    """Like ensure_valid_token, but recover a lost rotation before giving up.

    If our slot's refresh token is rejected (AuthError), the live `cfg/` dir may
    hold a token the SDK rotated that the poller never captured. sync_back pulls it
    into the slot; if that yields a fresh token we use it instead of flipping the
    account to needs-reauth. Idle accounts with no fresher copy re-raise, so the
    genuine-dead-token case still surfaces.
    """
    try:
        return ensure_valid_token(name)
    except oauth.AuthError:
        sync_back(name)
        cfg = load_config()
        blob = read_credentials(paths.slot_path(name))
        if _is_fresh(blob, cfg):
            return blob
        raise
```

```python
# src/ccswitch/accounts.py — in usage_for Step 1, call acquire_valid_token
    try:
        blob = acquire_valid_token(name)
    except oauth.TransientError as e:
        log.warning("usage_for(%s): transient refresh error, leaving status unchanged: %s", name, e)
        return None
    except oauth.AuthError as e:
        # ... (unchanged emit_auth_failure + set_status needs-reauth block from Task 3)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest tests/test_accounts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/accounts.py tests/test_accounts.py
git commit -m "fix(accounts): recover a lost rotation via sync_back before flipping needs-reauth

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Server — durable `account.needs_reauth` audit via queue-backed sink

**Files:**
- Create: `server/usage/auth_failure_audit.py`
- Modify: `server/app.py` (`_lifespan` ~line 101-164: register sink + start/stop drainer)
- Test: `server/tests/test_auth_failure_audit.py`

**Interfaces:**
- Consumes: `ccswitch.accounts.{AuthFailure, set_auth_failure_sink}`; `server.db._session_factory`;
  `server.audit.record_audit`.
- Produces:
  - `build_needs_reauth_detail(f: AuthFailure) -> dict` — pure mapping to the audit detail dict
    `{account, status, body, path, token_expires_at, attempts}`.
  - `class AuthFailureAuditor`: `.start()` registers the queue sink + launches the async drainer;
    `.stop()` unregisters + cancels. Drainer writes `account.needs_reauth` via `record_audit` and
    commits, one event per dequeued `AuthFailure`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_auth_failure_audit.py
import pytest
from ccswitch.accounts import AuthFailure
from server.usage.auth_failure_audit import build_needs_reauth_detail


def test_build_detail_maps_fields():
    f = AuthFailure(account="primary", status=400, body='{"error":"invalid_grant"}',
                    path="poller-idle", token_expires_at=123, attempts=1)
    d = build_needs_reauth_detail(f)
    assert d == {"account": "primary", "status": 400, "body": '{"error":"invalid_grant"}',
                 "path": "poller-idle", "token_expires_at": 123, "attempts": 1}


@pytest.mark.asyncio
async def test_drainer_writes_redacted_event(db_session, monkeypatch):
    # db_session: existing async-session fixture in server/tests/conftest.py
    from server.usage.auth_failure_audit import AuthFailureAuditor
    from sqlalchemy import select
    from server.db.models import AuditEvent
    auditor = AuthFailureAuditor(identity="system",
                                 session_factory=lambda: db_session_ctx(db_session))
    # Drain one item synchronously via the internal write helper (no event loop race):
    f = AuthFailure(account="primary", status=400,
                    body='{"refreshToken":"sk-ant-LEAKMEsk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"}',
                    path="poller-idle", token_expires_at=1, attempts=1)
    await auditor._write(f)
    rows = (await db_session.execute(select(AuditEvent).where(AuditEvent.type == "account.needs_reauth"))).scalars().all()
    assert len(rows) == 1
    detail_str = str(rows[0].detail)
    assert "sk-ant-" not in detail_str  # redacted at the server boundary
    assert rows[0].detail["account"] == "primary"
```

> Note for the implementer: reuse the existing async DB-session fixture from
> `server/tests/conftest.py` (grep `async def db_session` / `_session_factory`). If the fixture
> name differs, adapt `db_session`/`db_session_ctx` to the existing helper; the assertion (one
> redacted `account.needs_reauth` row) is the contract.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_auth_failure_audit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.usage.auth_failure_audit'`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/usage/auth_failure_audit.py
"""Durable audit of needs-reauth flips emitted by the DB-free core.

The core (ccswitch.accounts) calls a sink with an AuthFailure at each flip; the
sink (registered here) enqueues it, and an async drainer writes an
`account.needs_reauth` AuditEvent through the redaction layer. Decoupling via a
thread-safe queue handles the sync-worker-thread → async-DB boundary cleanly.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
from typing import Any, Callable

from ccswitch.accounts import AuthFailure, set_auth_failure_sink
from server.audit import record_audit

log = logging.getLogger("server")


def build_needs_reauth_detail(f: AuthFailure) -> dict[str, Any]:
    return {
        "account": f.account, "status": f.status, "body": f.body,
        "path": f.path, "token_expires_at": f.token_expires_at, "attempts": f.attempts,
    }


class AuthFailureAuditor:
    def __init__(self, *, identity: str, session_factory: Callable[[], Any]):
        self._identity = identity
        self._session_factory = session_factory
        self._q: "queue.Queue[AuthFailure]" = queue.Queue(maxsize=256)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        set_auth_failure_sink(self._enqueue)
        self._task = asyncio.create_task(self._drain())

    def stop(self) -> None:
        set_auth_failure_sink(None)
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _enqueue(self, f: AuthFailure) -> None:
        # Called from a worker thread (poller). Never block / raise into the core.
        try:
            self._q.put_nowait(f)
        except queue.Full:
            log.warning("auth_failure_audit: queue full, dropping needs-reauth event for %s", f.account)

    async def _drain(self) -> None:
        while True:
            try:
                f = await asyncio.to_thread(self._q.get)
                await self._write(f)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("auth_failure_audit: drainer write failed", exc_info=True)

    async def _write(self, f: AuthFailure) -> None:
        async with self._session_factory() as db:
            await record_audit(db, "account.needs_reauth", self._identity, build_needs_reauth_detail(f))
            await db.commit()
```

```python
# server/app.py — inside _lifespan, after DB init + log filter install, before/with orchestrator start
    from server.db import _session_factory as _sf
    from server.usage.auth_failure_audit import AuthFailureAuditor
    _auth_auditor = AuthFailureAuditor(identity="system", session_factory=lambda: _sf())
    _auth_auditor.start()
    app.state.auth_failure_auditor = _auth_auditor
```

```python
# server/app.py — in the _lifespan shutdown half (after `yield`)
    with contextlib.suppress(Exception):
        app.state.auth_failure_auditor.stop()
```

> Implementer note: `server/app.py` already imports `contextlib` in many lifespans; if not, add
> `import contextlib`. `_session_factory` is the module global set by `init_db`; capture it after
> DB init so it is non-None.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_auth_failure_audit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/usage/auth_failure_audit.py server/app.py server/tests/test_auth_failure_audit.py
git commit -m "feat(server): durable account.needs_reauth audit via queue-backed core sink

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Second flip site — orchestrator emits the same event

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_mark_needs_reauth` ~line 2026-2035)
- Test: `server/tests/test_orchestrator_token_refresh.py`

**Interfaces:**
- Consumes: `ccswitch.accounts.AuthFailure`; `server.usage.auth_failure_audit.build_needs_reauth_detail`;
  `server.audit.record_audit`; `server.db._session_factory`.
- Produces: `_mark_needs_reauth` writes an `account.needs_reauth` AuditEvent (path=`live-session`,
  status=401 — the in-session 401 that survived a recompose) in addition to setting the ledger
  status. Audit write is best-effort (swallowed) and must not break the existing flip.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_token_refresh.py
@pytest.mark.asyncio
async def test_mark_needs_reauth_writes_audit_event(db_session, monkeypatch):
    from sqlalchemy import select
    from server.db.models import AuditEvent
    import server.sessions.orchestrator as orch_mod
    # Point the orchestrator's audit path at the test session factory.
    monkeypatch.setattr(orch_mod, "_session_factory", lambda: db_session_ctx(db_session), raising=False)
    orch = make_orchestrator()  # existing test helper in this file
    await orch._mark_needs_reauth_audit("primary")   # see impl note below
    rows = (await db_session.execute(
        select(AuditEvent).where(AuditEvent.type == "account.needs_reauth"))).scalars().all()
    assert len(rows) == 1 and rows[0].detail["path"] == "live-session"
```

> Implementer note: `_mark_needs_reauth` is currently sync. Split the audit write into an async
> helper `_mark_needs_reauth_audit(account)` that `_mark_needs_reauth` schedules
> (`asyncio.create_task`) so the existing sync callers are unchanged. Test the async helper
> directly (above) to avoid task-scheduling races. If `make_orchestrator`/`db_session` helpers
> have different names here, reuse whatever this test file already uses.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_orchestrator_token_refresh.py -k mark_needs_reauth_writes_audit -v`
Expected: FAIL — `AttributeError: ... has no attribute '_mark_needs_reauth_audit'`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/orchestrator.py — replace _mark_needs_reauth and add the async helper
def _mark_needs_reauth(self, account: str) -> None:
    """Propagate a recompose-failed in-session 401 to the ledger AND audit it."""
    try:
        from ccswitch import ledger
        with ledger.locked_ledger() as led:
            ledger.set_status(led, account, "needs-reauth")
    except Exception:
        log.warning("recover.mark_needs_reauth failed account=%s", account, exc_info=True)
    with contextlib.suppress(RuntimeError):  # no running loop in some sync callers/tests
        asyncio.create_task(self._mark_needs_reauth_audit(account))

async def _mark_needs_reauth_audit(self, account: str) -> None:
    from ccswitch.accounts import AuthFailure
    from server.audit import record_audit
    from server.usage.auth_failure_audit import build_needs_reauth_detail
    from server.db import _session_factory
    if _session_factory is None:
        return
    f = AuthFailure(account=account, status=401, body=None,
                    path="live-session", token_expires_at=None, attempts=1)
    try:
        async with _session_factory() as db:
            await record_audit(db, "account.needs_reauth", "system", build_needs_reauth_detail(f))
            await db.commit()
    except Exception:
        log.warning("mark_needs_reauth_audit failed account=%s", account, exc_info=True)
```

> Implementer note: ensure `import contextlib` and `import asyncio` exist at the top of
> `orchestrator.py` (grep first; both are very likely already imported).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_orchestrator_token_refresh.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_token_refresh.py
git commit -m "feat(server): audit the in-session needs-reauth flip too (one event shape)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Wire the poller `path` label + full-suite + lint gate

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_poller_usage_fn` ~line 214-218)
- Modify: `src/ccswitch/cli.py` (`usage_for(a["name"])` ~line 148 → pass `path="cli"`)
- Test: full backend suite + lint

**Interfaces:**
- Consumes: `usage_for(..., path=...)` (Task 3).
- Produces: poller calls `usage_for(name, allow_refresh=..., path="poller-idle")`; CLI passes
  `path="cli"`. No new public interface.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_token_refresh.py
def test_poller_usage_fn_tags_path_poller_idle(monkeypatch):
    import server.sessions.orchestrator as orch_mod
    seen = {}
    def fake_usage_for(name, *, allow_refresh=True, path="unknown"):
        seen["path"] = path; seen["name"] = name; return None
    monkeypatch.setattr(orch_mod.ccswitch_accounts, "usage_for", fake_usage_for)
    orch = make_orchestrator()
    orch._poller._usage_fn("primary")   # call the wired poller usage fn
    assert seen == {"path": "poller-idle", "name": "primary"}
```

> Implementer note: if the poller stores the fn under a different attribute, call it via whatever
> accessor the existing poller tests use; the contract is `path="poller-idle"` reaching `usage_for`.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_orchestrator_token_refresh.py -k tags_path -v`
Expected: FAIL — `path` is `"unknown"`, not `"poller-idle"`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/orchestrator.py — _poller_usage_fn
            def _poller_usage_fn(name: str):
                return ccswitch_accounts.usage_for(
                    name,
                    allow_refresh=not self.has_live_session(name),
                    path="poller-idle",
                )
```

```python
# src/ccswitch/cli.py — the accounts sweep (~line 148)
                accounts.usage_for(a["name"], path="cli")
```

- [ ] **Step 4: Run the full backend suite + lint**

```bash
PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest tests/ server/tests/ -q
<main>/.venv/bin/ruff check src/ccswitch server
<main>/.venv/bin/mypy src/ccswitch server   # mypy skips untyped bodies; no new errors
```
Expected: all green. (Known environmental failures `test_remote_mode_wiring`, x11/xvfb — see
project memory — are pre-existing and unrelated; confirm the count matches baseline.)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py src/ccswitch/cli.py server/tests/test_orchestrator_token_refresh.py
git commit -m "feat: tag needs-reauth flip path (poller-idle / cli) for audit attribution

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Completion

After Task 7 is green: push the branch and open a non-draft PR with auto-merge
(`gh pr merge <#> --squash --auto`), per the project completion workflow. PR body references this
plan and the design spec, and notes the two deferred follow-ups (body-based 400 classification;
poller 429 circuit breaker) as tracked next steps. Body-classification is the intended fast-follow
once a real failing-400 body appears in `account.needs_reauth` audit rows.
```
