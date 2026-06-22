# Session daemon A2.2a — async orchestrator facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the edge's session operations through a uniform **async** facade (`app.state.sessions`) over the unchanged in-process orchestrator, so A2.2b can later swap in a remote proxy without touching the lifespan or the orchestrator's tests.

**Architecture:** A new `InProcessOrchestratorAdapter` wraps the real `SessionOrchestrator` and exposes every edge-called operation as an async method (await-delegating the async ones, async-wrapping the sync ones, plus `usage()` over the poller and `pending_permissions()` over the permission registry). `app.state.orchestrator` stays the raw lifecycle owner; a new `app.state.sessions` holds the facade. The API/WS/accounts call sites repoint to the facade and `await` everything.

**Tech Stack:** Python 3.12, FastAPI/Starlette, pytest + pytest-asyncio.

## Global Constraints

- Tests run via: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <path> -q -p no:cacheprovider`. pytest-asyncio strict (`@pytest.mark.asyncio` on async tests).
- No new dependencies. Do NOT modify `server/sessions/orchestrator.py` — the real orchestrator stays sync/async exactly as today.
- **Every method on the facade is `async`.** Therefore every `orchestrator.<m>(...)` / `orch.<m>(...)` call in a repointed file MUST be `await`ed — audit each call and add `await` where it isn't already present (already-awaited calls keep their single `await`). A missed `await` returns a coroutine object and fails the suite loudly.
- The facade lives at `server/sessions/facade.py`; `app.state.orchestrator` stays the raw `SessionOrchestrator`; the facade is a NEW `app.state.sessions`. The lifespan is unchanged.
- The full `server/tests` suite hangs in a firewalled sandbox (it polls real accounts; project memory). Run the named test files per task; the full suite runs clean on GitHub CI.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: `InProcessOrchestratorAdapter`

**Files:**
- Create: `server/sessions/facade.py`
- Test: `server/tests/test_orchestrator_facade.py`

**Interfaces:**
- Produces: `class InProcessOrchestratorAdapter(orch)` with async methods that forward `*args, **kwargs` to the wrapped orchestrator: await-delegating `create_session`, `send_prompt`, `ensure_driver`, `archive_session`, `unarchive`, `permanent_delete`, `adopt_session`, `rename_session`, `list_archived`, `transcript_events`, `interrupt`, `set_permission_mode`, `cancel_queued`, `run_command`; async-wrapping (no await) `list_sessions`, `get_session`, `transcript_path`, `pending_questions`, `pop_pending_initial_prompt`, `pop_pending_initial_attachment_ids`, `resolve_permission`, `answer_question`, `command_catalog`, `subscribe_events`, `unsubscribe_events`; plus `async def usage(self, name)` → `await orch.poller.get(name)` and `async def pending_permissions(self, session_id)` → `orch._registry.pending(session_id)`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orchestrator_facade.py`:

```python
from unittest import mock
import pytest
from server.sessions.facade import InProcessOrchestratorAdapter


def _orch():
    orch = mock.MagicMock()
    orch.create_session = mock.AsyncMock(return_value="created")
    orch.get_session = mock.MagicMock(return_value="rec")          # sync on the real orch
    orch.resolve_permission = mock.MagicMock(return_value=True)    # sync
    orch.poller = mock.MagicMock()
    orch.poller.get = mock.AsyncMock(return_value={"usage": 1})
    orch._registry = mock.MagicMock()
    orch._registry.pending = mock.MagicMock(return_value=["p1"])
    return orch


@pytest.mark.asyncio
async def test_async_method_is_awaited_and_forwarded():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.create_session(name="S", account="a") == "created"
    orch.create_session.assert_awaited_once_with(name="S", account="a")


@pytest.mark.asyncio
async def test_sync_method_is_wrapped_awaitable():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.get_session("s1") == "rec"
    orch.get_session.assert_called_once_with("s1")
    assert await f.resolve_permission("s1", "p1", "allow", "me") is True


@pytest.mark.asyncio
async def test_usage_wraps_poller_get():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.usage("acct") == {"usage": 1}
    orch.poller.get.assert_awaited_once_with("acct")


@pytest.mark.asyncio
async def test_pending_permissions_wraps_registry():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.pending_permissions("s1") == ["p1"]
    orch._registry.pending.assert_called_once_with("s1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_facade.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.facade'`.

- [ ] **Step 3: Write minimal implementation**

Create `server/sessions/facade.py`:

```python
"""Async facade over the in-process SessionOrchestrator.

Presents every edge-called operation as an async method so the edge can talk to
one uniform interface — satisfied here by the in-process orchestrator, and in
A2.2b by a remote proxy. The wrapped orchestrator is unchanged (still sync/async
as it is today); this layer only adds async-ness at the edge boundary.

Each method forwards to the orchestrator and `_maybe_await`s the result, so it is
correct whether the underlying method is sync OR async — no per-method
sync/async classification to get wrong.
"""
import inspect


async def _maybe_await(result):
    if inspect.isawaitable(result):
        return await result
    return result


class InProcessOrchestratorAdapter:
    def __init__(self, orch):
        self._orch = orch

    async def create_session(self, *a, **k): return await _maybe_await(self._orch.create_session(*a, **k))
    async def send_prompt(self, *a, **k): return await _maybe_await(self._orch.send_prompt(*a, **k))
    async def ensure_driver(self, *a, **k): return await _maybe_await(self._orch.ensure_driver(*a, **k))
    async def archive_session(self, *a, **k): return await _maybe_await(self._orch.archive_session(*a, **k))
    async def unarchive(self, *a, **k): return await _maybe_await(self._orch.unarchive(*a, **k))
    async def permanent_delete(self, *a, **k): return await _maybe_await(self._orch.permanent_delete(*a, **k))
    async def adopt_session(self, *a, **k): return await _maybe_await(self._orch.adopt_session(*a, **k))
    async def rename_session(self, *a, **k): return await _maybe_await(self._orch.rename_session(*a, **k))
    async def list_archived(self, *a, **k): return await _maybe_await(self._orch.list_archived(*a, **k))
    async def transcript_events(self, *a, **k): return await _maybe_await(self._orch.transcript_events(*a, **k))
    async def interrupt(self, *a, **k): return await _maybe_await(self._orch.interrupt(*a, **k))
    async def set_permission_mode(self, *a, **k): return await _maybe_await(self._orch.set_permission_mode(*a, **k))
    async def cancel_queued(self, *a, **k): return await _maybe_await(self._orch.cancel_queued(*a, **k))
    async def run_command(self, *a, **k): return await _maybe_await(self._orch.run_command(*a, **k))
    async def list_sessions(self, *a, **k): return await _maybe_await(self._orch.list_sessions(*a, **k))
    async def get_session(self, *a, **k): return await _maybe_await(self._orch.get_session(*a, **k))
    async def transcript_path(self, *a, **k): return await _maybe_await(self._orch.transcript_path(*a, **k))
    async def pending_questions(self, *a, **k): return await _maybe_await(self._orch.pending_questions(*a, **k))
    async def pop_pending_initial_prompt(self, *a, **k): return await _maybe_await(self._orch.pop_pending_initial_prompt(*a, **k))
    async def pop_pending_initial_attachment_ids(self, *a, **k): return await _maybe_await(self._orch.pop_pending_initial_attachment_ids(*a, **k))
    async def resolve_permission(self, *a, **k): return await _maybe_await(self._orch.resolve_permission(*a, **k))
    async def answer_question(self, *a, **k): return await _maybe_await(self._orch.answer_question(*a, **k))
    async def command_catalog(self, *a, **k): return await _maybe_await(self._orch.command_catalog(*a, **k))
    async def subscribe_events(self, *a, **k): return await _maybe_await(self._orch.subscribe_events(*a, **k))
    async def unsubscribe_events(self, *a, **k): return await _maybe_await(self._orch.unsubscribe_events(*a, **k))

    # --- edge-boundary helpers that hide orchestrator internals ---
    async def usage(self, name): return await _maybe_await(self._orch.poller.get(name))
    async def pending_permissions(self, session_id): return await _maybe_await(self._orch._registry.pending(session_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_facade.py -q -p no:cacheprovider`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/facade.py server/tests/test_orchestrator_facade.py
git commit -m "feat(sessions): InProcessOrchestratorAdapter async facade

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Wire the facade + repoint the WS bridge

**Files:**
- Modify: `server/app.py` (orchestrator construction block ~line 142-144 and the `build_ws_router(...)` call ~line 185)
- Modify: `server/bridge/ws.py` (the `orchestrator.<m>(...)` call sites)
- Test: `server/tests/test_ws.py` (+ any other `test_ws*`/session-stream tests)

**Interfaces:**
- Consumes: `InProcessOrchestratorAdapter` (Task 1).
- Produces: `app.state.sessions` (the facade); `build_ws_router` receives the facade; the WS bridge awaits all session operations.

- [ ] **Step 1: Repoint app.py**

In `server/app.py`, after `app.state.orchestrator = orchestrator` (~line 144), add:

```python
    from server.sessions.facade import InProcessOrchestratorAdapter
    app.state.sessions = InProcessOrchestratorAdapter(orchestrator)
```

Change the WS router construction (~line 185) from `build_ws_router(orchestrator)` to:

```python
    ws_router = build_ws_router(app.state.sessions)
```

- [ ] **Step 2: Repoint ws.py to await the facade**

In `server/bridge/ws.py`, the local `orchestrator` parameter is now the async facade. Apply the await-sweep (Global Constraints): add `await` to every `orchestrator.<m>(...)` call that is not already awaited. Concretely, the currently-sync calls that need `await` added are at (audit each — line numbers approximate):
- `orchestrator.subscribe_events()` → `await orchestrator.subscribe_events()` (~line 91)
- `orchestrator.get_session(...)` → `await ...` (~line 161)
- `orchestrator.transcript_path(...)` → `await ...` (~line 182)
- `orchestrator.command_catalog(...)` → `await ...` (~line 193)
- `orchestrator.pending_questions(...)` → `await ...` (~line 212)
- `orchestrator.pop_pending_initial_prompt(...)` → `await ...` (~line 241)
- `orchestrator.pop_pending_initial_attachment_ids(...)` → `await ...` (~line 242)
- `orchestrator.resolve_permission(...)` → `await ...` (~line 315)
- `orchestrator.answer_question(...)` → `await ...` (~line 327)
- `orchestrator.unsubscribe_events(...)` → `await ...`

And the one private-internal access — `orchestrator._registry.pending(session_id)` (~line 199) — becomes:

```python
            for p in await orchestrator.pending_permissions(session_id):
```

The already-awaited calls (`ensure_driver`, `send_prompt`, `interrupt`, `set_permission_mode`, `cancel_queued`) keep their single `await` — do not double-await. After editing, grep the file to confirm no bare `orchestrator.` call remains un-awaited: `grep -nE "[^t] orchestrator\.[a-z]" server/bridge/ws.py` should show only `await orchestrator.` (or assignments), no sync calls.

- [ ] **Step 3: Run the WS suite to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ws.py -q -p no:cacheprovider`
Expected: PASS (all green — a missed `await` would surface as a coroutine/`AttributeError` failure).

If other WS-exercising test files exist (e.g. `test_initial_prompt_visible.py`, `test_ws_*`), run them too and confirm green.

- [ ] **Step 4: Commit**

```bash
git add server/app.py server/bridge/ws.py
git commit -m "feat(server): route WS bridge through the async sessions facade

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Repoint the session REST API

**Files:**
- Modify: `server/api/sessions.py` (`_get_orchestrator` ~line 121 + the `orch.<m>(...)` call sites)
- Test: `server/tests/test_api.py` (+ session-API test files)

**Interfaces:**
- Consumes: `app.state.sessions` (Task 2).
- Produces: the session REST handlers use the async facade and await every orchestrator call.

- [ ] **Step 1: Repoint the accessor + await the calls**

In `server/api/sessions.py`:
- Change `_get_orchestrator` (~line 121) to return the facade:

```python
def _get_orchestrator(request: Request):
    return request.app.state.sessions
```

- Apply the await-sweep: add `await` to every `orch.<m>(...)` call not already awaited. The currently-sync ones needing `await` are `orch.list_sessions(...)` (~lines 136, 298) and `orch.get_session(...)` (~lines 347, 365, 373, 387, 470). The already-awaited calls (`create_session`, `adopt_session`, `list_archived`, `rename_session`, `archive_session`, `unarchive`, `permanent_delete`, `transcript_events`) keep their single `await`. Confirm with `grep -nE "[^t] orch\.[a-z]" server/api/sessions.py` → only `await orch.` should remain.

- [ ] **Step 2: Run the session API suite**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_api.py -q -p no:cacheprovider`
Expected: PASS (all green).

- [ ] **Step 3: Commit**

```bash
git add server/api/sessions.py
git commit -m "feat(api): route session REST handlers through the async sessions facade

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Repoint the accounts REST API (poller → usage)

**Files:**
- Modify: `server/api/accounts.py` (`_get_orchestrator` ~line 72 + the two `orch.poller.get(name)` sites ~lines 121, 143)
- Test: `server/tests/test_accounts_*` / the accounts-API test file

**Interfaces:**
- Consumes: `app.state.sessions` + `usage()` (Tasks 1-2).
- Produces: accounts handlers use the facade; usage comes from `await sessions.usage(name)` instead of `orch.poller`.

- [ ] **Step 1: Repoint the accessor + usage calls**

In `server/api/accounts.py`:
- Change `_get_orchestrator` (~line 72) to return the facade:

```python
def _get_orchestrator(request: Request):
    return request.app.state.sessions
```

- Replace both `await orch.poller.get(name)` (~lines 121, 143) with:

```python
        usage = await orch.usage(name)
```

Confirm no remaining `.poller` access in the file: `grep -n "\.poller" server/api/accounts.py` → empty.

- [ ] **Step 2: Run the accounts API tests**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q -p no:cacheprovider -k "account"`
Expected: PASS (the account-API tests green).

- [ ] **Step 3: Commit**

```bash
git add server/api/accounts.py
git commit -m "feat(api): accounts usage via sessions.usage() facade (drop direct .poller)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Full verification + PR

**Files:** none (verification only).

- [ ] **Step 1: Run the touched suites + facade unit test**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_facade.py server/tests/test_ws.py server/tests/test_api.py server/tests/test_healthz.py -q -p no:cacheprovider`
Expected: all pass, output pristine.

- [ ] **Step 2: Confirm no stray un-awaited / `.poller` / `_registry` access in the edge**

Run:
```bash
grep -rnE "[^t] (orch|orchestrator)\.[a-z]" server/api/sessions.py server/api/accounts.py server/bridge/ws.py | grep -v "await " | grep -v "app.state" | grep -v "def "
grep -rn "\.poller\b" server/api/ server/bridge/
grep -rn "orchestrator\._registry\|orch\._registry" server/api/ server/bridge/
```
Expected: no bare un-awaited orchestrator calls; no `.poller`/`_registry` access in the edge layer.

- [ ] **Step 3: Lint + type-check**

Run:
```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/sessions/facade.py server/app.py server/bridge/ws.py server/api/sessions.py server/api/accounts.py server/tests/test_orchestrator_facade.py
PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/ --ignore-missing-imports
```
Expected: ruff clean; mypy success.

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --base master --title "feat(server): async orchestrator facade (A2.2a)" --body "<summary: app.state.sessions async facade over the unchanged in-process orchestrator; edge (api/ws/accounts) repointed + awaited; behavior-preserving (suites green). Enables the A2.2b remote swap. Link the spec.>"
```

- [ ] **Step 5: Wait for CI green, then merge**

Run `gh pr checks <num>` until `lint-and-test` + `frontend` pass, then `gh pr merge <num> --merge --delete-branch`.

---

## Notes for the implementer

- The facade methods forward `*args, **kwargs`, so they match the orchestrator's signatures automatically — do not restate parameter lists.
- The single behavioral invariant: **every edge call to the facade must be awaited.** The existing suites are the safety net; a missed `await` fails loudly (a coroutine where a value was expected).
- Do NOT change `server/sessions/orchestrator.py`. Do NOT touch the lifespan (`_lifespan` keeps using `app.state.orchestrator`). Do NOT change healthz (it reads `app.state.orchestrator.active_session_count()`).
- The full `server/tests` suite hangs in this firewalled sandbox; run the named files. CI runs everything.
