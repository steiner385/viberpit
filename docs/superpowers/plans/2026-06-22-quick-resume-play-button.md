# Quick-Resume Play Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a play button that fires a session's auto-generated next-step suggestion — in the composer (prompt line) and on each session card — to unstick stalled sessions in one tap.

**Architecture:** Reuse the existing suggestion generator (`server/sessions/suggestions.py`) and `suggestions` WS event. Cache the latest generated items on the driver, read them through the orchestrator + facade, surface them in the session-list payload, add an on-demand `request_suggestions` trigger, and add a REST `POST /sessions/{id}/prompt` so the dashboard can fire a suggestion without holding the session's live WebSocket. Frontend adds a play button (+ caret menu for runner-up suggestions) to `Composer.tsx` and `SessionCard.tsx`.

**Tech Stack:** Python FastAPI + SQLAlchemy (backend) · React + Vite + TS + Tailwind + lucide-react + React Query (frontend) · vitest / pytest.

## Global Constraints

- **No new generator / no parallel implementation.** One generator (`SuggestionService.generate`), one event (`ev.Suggestions`), one render path. We add *triggers* and *readers*, never a second source of the draft.
- **No DB migration.** Suggestions are ephemeral (live-session only); cache them in memory on the driver. Do **not** add a `Session` model column.
- **Facade parity (in-process).** Every new orchestrator method MUST get a one-line delegating wrapper in `server/sessions/facade.py` (`InProcessOrchestratorAdapter`), because both the REST API and WS bridge call through `app.state.sessions`, never the raw orchestrator.
- **Icons / styling.** Use `lucide-react` (`Play`, `Loader2`, `ChevronUp`/`MoreHorizontal`). Reuse `cn()` and the existing `actionBtn` style in `SessionCard.tsx`; match the existing composer button classes.
- **Test auth.** API tests 401 unless the tailnet identity is allowlisted. Run server tests with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/...`. Web tests run in `web/` via `vitest run` (symlink `web/node_modules` from the main checkout in a fresh worktree).
- **Deferred (NOT in this plan) — see "Deferred work" at the end:** remote-daemon (`RemoteOrchestrator` + IPC) parity for the two new orchestrator methods; auto-fire-on-open for the card's "no suggestion" case.

---

## File structure

**Backend (modify):**
- `server/sessions/driver.py` — cache `_last_suggestions`; add `request_suggestions()`.
- `server/sessions/orchestrator.py` — add `latest_suggestions()` and `request_suggestions()`.
- `server/sessions/facade.py` — delegating wrappers for both new methods.
- `server/bridge/ws.py` — dispatch `request_suggestions` message.
- `server/api/sessions.py` — `latestSuggestions` in payload; `POST /{id}/prompt`.

**Frontend (modify):**
- `web/src/api/types.ts` — `latestSuggestions?: string[]` on `Session`.
- `web/src/ws/client.ts` — `requestSuggestions()` method + hook export.
- `web/src/session/useSessionRuntime.ts` — `onRequestSuggestions` in runtime.
- `web/src/components/session/SessionView.tsx` — pass `onRequestSuggestions` to Composer.
- `web/src/components/session/Composer.tsx` — play button + caret menu + generate-and-prefill.
- `web/src/api/hooks.ts` — `useSendPrompt()` mutation.
- `web/src/components/SessionCard.tsx` — play button + caret; `onQuickResume` prop.
- `web/src/components/Dashboard.tsx` — wire `onQuickResume` (fire-in-place vs navigate).

---

## Task 1: Cache latest suggestions (driver) + orchestrator reader

**Files:**
- Modify: `server/sessions/driver.py` (`_run_suggestions`, ~lines 426-429)
- Modify: `server/sessions/orchestrator.py` (add method near `interrupt`, ~line 972)
- Modify: `server/sessions/facade.py`
- Test: `server/tests/test_suggestion_cache.py` (create)

**Interfaces:**
- Produces: `SessionDriver._last_suggestions: list[str]` (set in `_run_suggestions`); `SessionOrchestrator.latest_suggestions(session_id: str) -> list[str]`; `InProcessOrchestratorAdapter.latest_suggestions(session_id) -> list[str]` (async).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_suggestion_cache.py
import asyncio
import types
from unittest.mock import AsyncMock

from server.sessions.driver import SessionDriver
from server.sessions.orchestrator import SessionOrchestrator


async def _run_caches():
    fake = types.SimpleNamespace(
        _suggester=types.SimpleNamespace(generate=AsyncMock(return_value=["Run the tests", "Commit it"])),
        cwd="/x", config_dir="/c", session_id="s1",
        _out=asyncio.Queue(), _last_suggestions=[],
    )
    await SessionDriver._run_suggestions(fake, "User: hi\n\nAssistant: done", "t1")
    return fake._last_suggestions


def test_run_suggestions_caches_items():
    assert asyncio.run(_run_caches()) == ["Run the tests", "Commit it"]


def test_orchestrator_latest_suggestions_reads_driver():
    orch = SessionOrchestrator()
    orch._drivers["s1"] = types.SimpleNamespace(_last_suggestions=["A", "B"])
    assert orch.latest_suggestions("s1") == ["A", "B"]
    assert orch.latest_suggestions("missing") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_suggestion_cache.py -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'latest_suggestions'` (and the caching assert fails).

- [ ] **Step 3: Cache items in `_run_suggestions`**

In `server/sessions/driver.py`, `_run_suggestions` currently reads:

```python
    async def _run_suggestions(self, context: str, turn_id: str) -> None:
        items = await self._suggester.generate(self.cwd, self.config_dir, context)
        log.info("suggestions session=%s turn=%s n=%d", self.session_id, turn_id, len(items))
        if items:
            await self._out.put(ev.Suggestions(self.session_id, turn_id, items))
```

Change the body to cache before emitting:

```python
    async def _run_suggestions(self, context: str, turn_id: str) -> None:
        items = await self._suggester.generate(self.cwd, self.config_dir, context)
        log.info("suggestions session=%s turn=%s n=%d", self.session_id, turn_id, len(items))
        if items:
            self._last_suggestions = items
            await self._out.put(ev.Suggestions(self.session_id, turn_id, items))
```

(No `__init__` change needed — the orchestrator reader uses `getattr(..., [])`.)

- [ ] **Step 4: Add the orchestrator reader**

In `server/sessions/orchestrator.py`, after the `interrupt` method (~line 975), add:

```python
    def latest_suggestions(self, session_id: str) -> list[str]:
        """Latest cached next-prompt suggestions for a live session.

        Returns [] when the session has no live driver (detached) or none were
        generated. Read straight off the driver — suggestions are ephemeral and
        never persisted (see suggestions.py)."""
        d = self._drivers.get(session_id)
        return list(getattr(d, "_last_suggestions", []) or []) if d else []
```

- [ ] **Step 5: Add the facade wrapper**

In `server/sessions/facade.py`, add inside `InProcessOrchestratorAdapter` (after the `get_session` line, ~line 40):

```python
    async def latest_suggestions(self, *a, **k): return await _maybe_await(self._orch.latest_suggestions(*a, **k))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_suggestion_cache.py -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add server/sessions/driver.py server/sessions/orchestrator.py server/sessions/facade.py server/tests/test_suggestion_cache.py
git commit -m "feat(suggestions): cache latest items on driver + orchestrator reader"
```

---

## Task 2: On-demand suggestion generation trigger

**Files:**
- Modify: `server/sessions/driver.py` (add `request_suggestions`, near `_maybe_start_suggestions` ~line 412)
- Modify: `server/sessions/orchestrator.py` (add `request_suggestions`)
- Modify: `server/sessions/facade.py`
- Modify: `server/bridge/ws.py` (dispatch loop, after the `interrupt` branch ~line 442)
- Test: extend `server/tests/test_suggestion_cache.py`

**Interfaces:**
- Consumes: `SessionDriver._maybe_start_suggestions()` (existing, gated on pending permission/question; builds context from `_last_user_text` / `_last_assistant_parts`).
- Produces: `SessionDriver.request_suggestions() -> None`; `SessionOrchestrator.request_suggestions(session_id) -> None` (async); facade wrapper; WS message type `request_suggestions`.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_suggestion_cache.py
def test_orchestrator_request_suggestions_calls_driver():
    orch = SessionOrchestrator()
    calls = []
    orch._drivers["s1"] = types.SimpleNamespace(request_suggestions=lambda: calls.append("s1"))
    asyncio.run(orch.request_suggestions("s1"))
    assert calls == ["s1"]
    # Unknown session is a no-op, not an error.
    asyncio.run(orch.request_suggestions("missing"))
    assert calls == ["s1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_suggestion_cache.py::test_orchestrator_request_suggestions_calls_driver -v`
Expected: FAIL — `AttributeError: ... has no attribute 'request_suggestions'`.

- [ ] **Step 3: Add the driver method**

In `server/sessions/driver.py`, immediately after `_maybe_start_suggestions` (~line 423), add:

```python
    def request_suggestions(self) -> None:
        """On-demand regeneration of next-prompt suggestions (best-effort).

        Reuses the same gated, context-from-last-turn path as the automatic
        turn-completion trigger; a no-op if no suggester is configured or a
        permission/question is pending."""
        self._maybe_start_suggestions()
```

- [ ] **Step 4: Add the orchestrator method**

In `server/sessions/orchestrator.py`, after `latest_suggestions` (from Task 1), add:

```python
    async def request_suggestions(self, session_id: str) -> None:
        """Ask a live session to (re)generate next-prompt suggestions now."""
        d = self._drivers.get(session_id)
        if d:
            d.request_suggestions()
```

- [ ] **Step 5: Add the facade wrapper**

In `server/sessions/facade.py`, after the `latest_suggestions` line:

```python
    async def request_suggestions(self, *a, **k): return await _maybe_await(self._orch.request_suggestions(*a, **k))
```

- [ ] **Step 6: Dispatch the WS message**

In `server/bridge/ws.py`, in the message-dispatch chain, add a branch after the `interrupt` block (after ~line 442, before `permissionResponse`):

```python
        elif msg_type == "request_suggestions":
            await orchestrator.request_suggestions(session_id)
```

- [ ] **Step 7: Run orchestrator test to verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_suggestion_cache.py -v`
Expected: PASS (3 tests).

- [ ] **Step 8: Add WS-dispatch coverage**

Open `server/tests/test_ws.py`, find the test that exercises `msg_type == "interrupt"` (asserts the fake orchestrator's `interrupt` was called for a session). Copy it to a new test `test_request_suggestions_dispatch`, change the sent frame to `{"type": "request_suggestions", "sessionId": <sid>}`, and assert the fake orchestrator's `request_suggestions` was awaited with that `sessionId`. If the fake orchestrator is a stub class, add an async `request_suggestions` method that records the call. Run:

`env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_ws.py -v`
Expected: PASS. (Per project memory: ensure `CCSWITCH_ENABLE_XVFB` is unset for `test_ws.py`.)

- [ ] **Step 9: Commit**

```bash
git add server/sessions/driver.py server/sessions/orchestrator.py server/sessions/facade.py server/bridge/ws.py server/tests/test_suggestion_cache.py server/tests/test_ws.py
git commit -m "feat(suggestions): on-demand request_suggestions trigger (WS + facade)"
```

---

## Task 3: Surface latestSuggestions in the session-list payload

**Files:**
- Modify: `server/api/sessions.py` (`_record_to_dict` ~line 85; `list_sessions` route ~line 141)
- Test: `server/tests/test_sessions_payload.py` (create)

**Interfaces:**
- Consumes: `orch.latest_suggestions(session_id)` (Task 1).
- Produces: `_record_to_dict(record, repo=_UNSET, suggestions=None)` now includes `"latestSuggestions"`; `GET /api/sessions` items include `latestSuggestions: string[]`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_sessions_payload.py
from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord


def _rec():
    return SessionRecord(id="s1", name="n", account="a", claude_session_id="c",
                         cols=80, rows=24, cwd="/x")


def test_record_to_dict_includes_suggestions():
    d = _record_to_dict(_rec(), repo=None, suggestions=["Run the tests"])
    assert d["latestSuggestions"] == ["Run the tests"]


def test_record_to_dict_defaults_empty_suggestions():
    d = _record_to_dict(_rec(), repo=None)
    assert d["latestSuggestions"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_payload.py -v`
Expected: FAIL — `KeyError: 'latestSuggestions'`.

- [ ] **Step 3: Extend `_record_to_dict`**

In `server/api/sessions.py`, change the signature and add the key:

```python
def _record_to_dict(record, repo: Any = _UNSET, suggestions: list[str] | None = None) -> dict[str, Any]:
```

Add this entry to the returned dict (after the `"model"` line):

```python
        "model": getattr(record, "model", None),
        "latestSuggestions": list(suggestions or []),
    }
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_payload.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Populate suggestions in the list route**

In `server/api/sessions.py`, update the `list_sessions` route body:

```python
@router.get("")
async def list_sessions(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """Return all in-memory session records."""
    orch = _get_orchestrator(request)
    records = await orch.list_sessions()
    # Read each session's repo (live, from its cwd) and cached suggestions in parallel.
    repos = await asyncio.gather(*(_resolve_repo(r) for r in records))
    suggs = await asyncio.gather(*(orch.latest_suggestions(r.id) for r in records))
    return [
        _record_to_dict(r, repo=repo, suggestions=s)
        for r, repo, s in zip(records, repos, suggs)
    ]
```

- [ ] **Step 6: Add a list-route integration assertion**

Open `server/tests/` and find the existing test for `GET /api/sessions` (search for `"/api/sessions"` or `list_sessions`). Add an assertion to (or alongside) that test that every returned item contains the key `latestSuggestions` and that it is a list. Run:

`env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/ -k sessions -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_payload.py
git commit -m "feat(api): surface latestSuggestions in the sessions list payload"
```

---

## Task 4: REST POST /sessions/{id}/prompt (send-from-list)

**Files:**
- Modify: `server/api/sessions.py` (add `SendPromptRequest` near line 57; add route near the unarchive route ~line 418)
- Test: `server/tests/test_send_prompt_route.py` (create)

**Interfaces:**
- Consumes: `orch.get_session`, `orch.send_prompt`, `orch.latest_suggestions`; `SessionRecord.detached`.
- Produces: `POST /api/sessions/{id}/prompt` with body `{ "text": str }` → `_record_to_dict`. 404 unknown session, 409 detached (not live).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_send_prompt_route.py
import types
import pytest
from httpx import ASGITransport, AsyncClient

from server.app import create_app


class _FakeOrch:
    def __init__(self, record, detached=False):
        self._record = record
        self.sent = []
    async def get_session(self, sid):
        return self._record if (self._record and self._record.id == sid) else None
    async def send_prompt(self, sid, text):
        self.sent.append((sid, text))
    async def latest_suggestions(self, sid):
        return []


def _rec(detached=False):
    from server.sessions.types import SessionRecord
    return SessionRecord(id="s1", name="n", account="a", claude_session_id="c",
                         cols=80, rows=24, cwd="/x", detached=detached)


@pytest.mark.anyio
async def test_send_prompt_200_live(monkeypatch):
    app = create_app()
    fake = _FakeOrch(_rec(detached=False))
    app.state.sessions = fake
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/sessions/s1/prompt", json={"text": "continue"})
    assert r.status_code == 200
    assert fake.sent == [("s1", "continue")]


@pytest.mark.anyio
async def test_send_prompt_409_detached(monkeypatch):
    app = create_app()
    app.state.sessions = _FakeOrch(_rec(detached=True))
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/sessions/s1/prompt", json={"text": "continue"})
    assert r.status_code == 409


@pytest.mark.anyio
async def test_send_prompt_404_unknown(monkeypatch):
    app = create_app()
    app.state.sessions = _FakeOrch(None)
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/sessions/missing/prompt", json={"text": "x"})
    assert r.status_code == 404
```

> Note: if existing route tests use a shared fixture/helper for app + auth (search `server/tests/` for `create_app` or an `app` fixture and the allowlist setup), mirror that instead of the inline `_FakeOrch`/env approach above. The auth identity must be allowlisted or `CCSWITCH_DEV=1` set so the request isn't 401.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_send_prompt_route.py -v`
Expected: FAIL — 404/405 (route not defined).

- [ ] **Step 3: Add the request model**

In `server/api/sessions.py`, after `RenameSessionRequest` (~line 67):

```python
class SendPromptRequest(BaseModel):
    text: str
```

- [ ] **Step 4: Add the route**

In `server/api/sessions.py`, after the `unarchive_session` route (~line 441):

```python
@router.post("/{session_id}/prompt")
async def send_session_prompt(
    session_id: str,
    body: SendPromptRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Send a prompt to a live session from the dashboard (quick-resume).

    404 if unknown; 409 if the session is detached (no live driver) — the client
    should open the session, which resumes it lazily, and send from there."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    if getattr(record, "detached", False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "not_live", "message": "Session is not live; open it to resume."},
        )
    await orch.send_prompt(session_id, body.text)
    await record_audit(db, "session.prompt", identity, {"sessionId": session_id})
    return _record_to_dict(record, repo=await _resolve_repo(record))
```

- [ ] **Step 5: Run tests to verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_send_prompt_route.py -v`
Expected: PASS (3 tests). If 401, align the test's auth setup with the existing route-test fixture (see Step 1 note).

- [ ] **Step 6: Commit**

```bash
git add server/api/sessions.py server/tests/test_send_prompt_route.py
git commit -m "feat(api): POST /sessions/{id}/prompt for dashboard quick-resume"
```

---

## Task 5: Frontend wiring — type, WS method, runtime callback

**Files:**
- Modify: `web/src/api/types.ts` (`Session`, ~line 61)
- Modify: `web/src/ws/client.ts` (method ~line 147; hook export ~line 317+)
- Modify: `web/src/session/useSessionRuntime.ts` (destructure + `onRequestSuggestions`)
- Modify: `web/src/components/session/SessionView.tsx` (pass prop ~line 145)
- Test: `web/src/ws/client.test.ts` (extend existing, or create)

**Interfaces:**
- Produces: `Session.latestSuggestions?: string[]`; `SessionSocket.requestSuggestions(sessionId: string): void`; `useSessionSocket().requestSuggestions`; `SessionRuntime.onRequestSuggestions: () => void`.

- [ ] **Step 1: Write the failing test**

Find the existing test that asserts `socket.prompt(...)` writes a frame (search `web/src/ws/` for `prompt` in a `.test.ts`). Mirror it:

```typescript
// web/src/ws/client.test.ts (add)
it('requestSuggestions sends a request_suggestions frame', () => {
  const sent: string[] = []
  const socket = makeTestSocket(sent)   // reuse the helper the prompt test uses
  socket.requestSuggestions('s1')
  expect(JSON.parse(sent.at(-1)!)).toEqual({ type: 'request_suggestions', sessionId: 's1' })
})
```

If there is no existing socket-frame test/helper, assert against a mock `WebSocket` whose `send` pushes into `sent`, constructing the socket the same way the module's other tests do.

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/ws/client.test.ts`
Expected: FAIL — `socket.requestSuggestions is not a function`.

- [ ] **Step 3: Add the type field**

In `web/src/api/types.ts`, in `interface Session`, after the `model` line:

```typescript
  /** Cached next-prompt suggestions for a live session (empty when none/detached). */
  latestSuggestions?: string[]
```

- [ ] **Step 4: Add the socket method**

In `web/src/ws/client.ts`, after the `prompt` method (~line 147):

```typescript
  requestSuggestions(sessionId: string) {
    this._send({ type: 'request_suggestions', sessionId })
  }
```

- [ ] **Step 5: Export from the hook**

In `web/src/ws/client.ts`, in `useSessionSocket()` (mirror the `prompt` wrap at ~line 337), add to the returned callbacks:

```typescript
  const requestSuggestions = useCallback((sessionId: string) => socket.requestSuggestions(sessionId), [socket])
```

and include `requestSuggestions` in the returned object alongside `prompt`.

- [ ] **Step 6: Expose on the runtime**

In `web/src/session/useSessionRuntime.ts`: add `requestSuggestions` to the `useSessionSocket()` destructure (~line 67-80), then add near `onPrompt` (~line 225):

```typescript
  const onRequestSuggestions = useCallback(
    () => requestSuggestions(sessionId),
    [requestSuggestions, sessionId],
  )
```

Add `onRequestSuggestions: () => void` to the `SessionRuntime` interface (~line 26-64) and include `onRequestSuggestions` in the returned runtime object.

- [ ] **Step 7: Pass into the Composer**

In `web/src/components/session/SessionView.tsx`, where `<Composer ... />` is rendered (~line 132-148), add the prop:

```typescript
            onRequestSuggestions={rt.onRequestSuggestions}
```

- [ ] **Step 8: Run test + typecheck to verify pass**

Run (in `web/`): `npx vitest run src/ws/client.test.ts && npx tsc --noEmit`
Expected: vitest PASS; tsc clean (Composer prop is added in Task 6 — if tsc flags a missing `onRequestSuggestions` prop on Composer here, add the prop to `ComposerProps` now as `onRequestSuggestions: () => void` and a no-op default in tests).

- [ ] **Step 9: Commit**

```bash
git add web/src/api/types.ts web/src/ws/client.ts web/src/session/useSessionRuntime.ts web/src/components/session/SessionView.tsx web/src/ws/client.test.ts
git commit -m "feat(web): requestSuggestions socket method + runtime callback + Session.latestSuggestions"
```

---

## Task 6: Composer play button + caret menu

**Files:**
- Modify: `web/src/components/session/Composer.tsx`
- Test: `web/src/components/session/Composer.test.tsx` (extend existing)

**Interfaces:**
- Consumes: `suggestions: string[]` (already a prop), `onPrompt(text, attachmentIds)` (already a prop), `onRequestSuggestions: () => void` (Task 5), `prefill` (existing ~line 121), `isWorking` (existing).
- Produces: a play button + optional caret menu in the composer action row.

- [ ] **Step 1: Write the failing tests**

Mirror the existing Composer test setup (find the render helper / default props in `Composer.test.tsx`). Add:

```typescript
// web/src/components/session/Composer.test.tsx (add)
import { render, screen, fireEvent } from '@testing-library/react'

it('shows the play button only when idle and the box is empty', () => {
  const { rerender } = renderComposer({ status: 'done', suggestions: ['Run the tests'] })
  expect(screen.getByRole('button', { name: /resume/i })).toBeInTheDocument()
  rerender(<Composer {...defaultProps({ status: 'in_progress', suggestions: ['x'] })} />)
  expect(screen.queryByRole('button', { name: /resume/i })).not.toBeInTheDocument()
})

it('existing suggestion: play sends it immediately', () => {
  const onPrompt = vi.fn()
  renderComposer({ status: 'done', suggestions: ['Run the tests', 'Commit it'], onPrompt })
  fireEvent.click(screen.getByRole('button', { name: /resume/i }))
  expect(onPrompt).toHaveBeenCalledWith('Run the tests', [])
})

it('no suggestion: play requests generation and does not send', () => {
  const onPrompt = vi.fn()
  const onRequestSuggestions = vi.fn()
  renderComposer({ status: 'done', suggestions: [], onPrompt, onRequestSuggestions })
  fireEvent.click(screen.getByRole('button', { name: /resume/i }))
  expect(onRequestSuggestions).toHaveBeenCalledTimes(1)
  expect(onPrompt).not.toHaveBeenCalled()
})

it('generated suggestion prefills the box (confirm, not auto-send)', () => {
  const onPrompt = vi.fn()
  const { rerender } = renderComposer({ status: 'done', suggestions: [], onRequestSuggestions: vi.fn(), onPrompt })
  fireEvent.click(screen.getByRole('button', { name: /resume/i }))
  rerender(<Composer {...defaultProps({ status: 'done', suggestions: ['Finish the task'], onPrompt })} />)
  expect(screen.getByLabelText('Message')).toHaveValue('Finish the task')
  expect(onPrompt).not.toHaveBeenCalled()
})

it('caret menu appears with >=2 suggestions and picking sends that item', () => {
  const onPrompt = vi.fn()
  renderComposer({ status: 'done', suggestions: ['First', 'Second'], onPrompt })
  fireEvent.click(screen.getByRole('button', { name: /more suggestions/i }))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Second' }))
  expect(onPrompt).toHaveBeenCalledWith('Second', [])
})
```

(Define `renderComposer(overrides)` / `defaultProps(overrides)` by reusing whatever the file already uses for required Composer props; add `onRequestSuggestions: vi.fn()` to the defaults.)

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `npx vitest run src/components/session/Composer.test.tsx`
Expected: FAIL — no element with name /resume/.

- [ ] **Step 3: Add prop, state, and handlers**

In `web/src/components/session/Composer.tsx`:

Add to the props interface: `onRequestSuggestions: () => void`.

Add state near the other `useState` hooks:

```typescript
  const [pendingGenerate, setPendingGenerate] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
```

Add the handlers (near `prefill`, ~line 130):

```typescript
  const showPlay = !isWorking && text === ''

  const play = useCallback(() => {
    if (suggestions && suggestions.length > 0) {
      onPrompt(suggestions[0], [])
      return
    }
    setPendingGenerate(true)
    onRequestSuggestions()
  }, [suggestions, onPrompt, onRequestSuggestions])

  // A freshly generated suggestion prefills the box for confirmation (never auto-sends).
  useEffect(() => {
    if (pendingGenerate && suggestions && suggestions.length > 0) {
      prefill(suggestions[0])
      setPendingGenerate(false)
    }
  }, [pendingGenerate, suggestions, prefill])

  // Quiet-fail: if generation produces nothing within 8s, drop the spinner.
  useEffect(() => {
    if (!pendingGenerate) return
    const t = setTimeout(() => setPendingGenerate(false), 8000)
    return () => clearTimeout(t)
  }, [pendingGenerate])
```

Ensure `useEffect`, `useState`, `useCallback` and the lucide icons are imported:

```typescript
import { Play, Loader2, ChevronUp } from 'lucide-react'
```

- [ ] **Step 4: Render the play button + caret**

In the composer action row (next to the Send button, ~lines 343-356), insert before the Send button:

```typescript
        {showPlay && (
          <div className="relative flex items-center">
            <button
              type="button"
              aria-label="Resume (send suggested next step)"
              title="Resume — send the suggested next step"
              onClick={play}
              disabled={pendingGenerate}
              className={cn(
                'flex-shrink-0 rounded-md px-3 py-2 text-sm font-medium min-h-[44px]',
                'bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-60',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400',
              )}
            >
              {pendingGenerate ? <Loader2 size={16} className="animate-spin" aria-hidden="true" />
                               : <Play size={16} aria-hidden="true" />}
            </button>
            {suggestions && suggestions.length >= 2 && (
              <button
                type="button"
                aria-label="More suggestions"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((o) => !o)}
                className={cn(
                  'flex-shrink-0 rounded-md px-1 py-2 min-h-[44px] text-emerald-700 dark:text-emerald-400',
                  'hover:bg-emerald-50 dark:hover:bg-emerald-900/20',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400',
                )}
              >
                <ChevronUp size={16} aria-hidden="true" />
              </button>
            )}
            {menuOpen && suggestions && suggestions.length >= 2 && (
              <div
                role="menu"
                className={cn(
                  'absolute bottom-full right-0 mb-1 min-w-[12rem] max-w-xs z-10',
                  'rounded-md border border-gray-200 dark:border-gray-700',
                  'bg-white dark:bg-gray-800 shadow-lg py-1',
                )}
              >
                {suggestions.slice(1).map((s, i) => (
                  <button
                    key={i}
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); onPrompt(s, []) }}
                    className={cn(
                      'block w-full truncate px-3 py-2 text-left text-sm',
                      'hover:bg-gray-100 dark:hover:bg-gray-700',
                    )}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
```

- [ ] **Step 5: Run tests + typecheck to verify pass**

Run (in `web/`): `npx vitest run src/components/session/Composer.test.tsx && npx tsc --noEmit`
Expected: PASS; tsc clean.

- [ ] **Step 6: Lint + commit**

```bash
cd web && npx eslint src/components/session/Composer.tsx && cd ..
git add web/src/components/session/Composer.tsx web/src/components/session/Composer.test.tsx
git commit -m "feat(web): play button + suggestion caret menu in the composer"
```

---

## Task 7: useSendPrompt mutation hook

**Files:**
- Modify: `web/src/api/hooks.ts` (near `useRenameSession` ~line 143)
- Test: `web/src/api/hooks.test.ts` (extend existing, or create)

**Interfaces:**
- Produces: `useSendPrompt()` → mutation with `mutate({ id, text })` → `POST /sessions/{id}/prompt`.

- [ ] **Step 1: Write the failing test**

Mirror the existing mutation test for `useRenameSession` (find it in `web/src/api/hooks.test.ts`). Add:

```typescript
it('useSendPrompt POSTs to /sessions/{id}/prompt', async () => {
  const post = vi.spyOn(api, 'post').mockResolvedValue({} as Session)
  const { result } = renderHook(() => useSendPrompt(), { wrapper })
  result.current.mutate({ id: 's1', text: 'continue' })
  await waitFor(() => expect(post).toHaveBeenCalledWith('/sessions/s1/prompt', { text: 'continue' }))
})
```

(Reuse the `wrapper` / QueryClient setup the other hook tests use.)

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/api/hooks.test.ts`
Expected: FAIL — `useSendPrompt is not exported`.

- [ ] **Step 3: Add the hook**

In `web/src/api/hooks.ts`, after `useRenameSession` (~line 149):

```typescript
export function useSendPrompt() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; text: string }>({
    mutationFn: ({ id, text }) => api.post<Session>(`/sessions/${id}/prompt`, { text }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/api/hooks.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/hooks.ts web/src/api/hooks.test.ts
git commit -m "feat(web): useSendPrompt mutation for dashboard quick-resume"
```

---

## Task 8: SessionCard play button + caret + Dashboard wiring

**Files:**
- Modify: `web/src/components/SessionCard.tsx` (props ~line 18; action row ~line 159)
- Modify: `web/src/components/Dashboard.tsx` (SessionCard render ~line 1489; add handler)
- Test: `web/src/components/SessionCard.test.tsx` (extend/create)

**Interfaces:**
- Consumes: `session.latestSuggestions`, `session.detached`, `onOpen` (existing), `useSendPrompt` (Task 7).
- Produces: `SessionCardProps.onQuickResume: (id: string, text?: string) => void`. `text` present → fire in place; `text` undefined → caller navigates.

- [ ] **Step 1: Write the failing tests**

```typescript
// web/src/components/SessionCard.test.tsx (add; reuse existing renderCard helper/defaults)
it('live session with a suggestion: play fires in place with the first suggestion', () => {
  const onQuickResume = vi.fn()
  renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['Run the tests'] }), onQuickResume })
  fireEvent.click(screen.getByRole('button', { name: /resume/i }))
  expect(onQuickResume).toHaveBeenCalledWith('s1', 'Run the tests')
})

it('no suggestion: play calls onQuickResume with no text (caller navigates)', () => {
  const onQuickResume = vi.fn()
  renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume })
  fireEvent.click(screen.getByRole('button', { name: /resume/i }))
  expect(onQuickResume).toHaveBeenCalledWith('s1', undefined)
})

it('detached session with a suggestion: play passes no text (navigate to resume)', () => {
  const onQuickResume = vi.fn()
  renderCard({ session: mkSession({ status: 'done', detached: true, latestSuggestions: ['x'] }), onQuickResume })
  fireEvent.click(screen.getByRole('button', { name: /resume/i }))
  expect(onQuickResume).toHaveBeenCalledWith('s1', undefined)
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `npx vitest run src/components/SessionCard.test.tsx`
Expected: FAIL — no /resume/ button.

- [ ] **Step 3: Add the prop + button to SessionCard**

In `web/src/components/SessionCard.tsx`, add to `SessionCardProps`:

```typescript
  /** Quick-resume: text present → fire that suggestion in place; undefined → caller opens the session. */
  onQuickResume: (id: string, text?: string) => void
```

Destructure `onQuickResume` in the component signature. Add (with the `Play`/`ChevronUp` imports) a derived value and the button as the **first** control in the action-button row (before Rename, ~line 159):

```typescript
  const suggestions = session.latestSuggestions ?? []
  const canFireInPlace = suggestions.length > 0 && !session.detached
```

```typescript
        <button
          type="button"
          className={cn(actionBtn, 'text-emerald-600 dark:text-emerald-400',
            'hover:bg-emerald-50 dark:hover:bg-emerald-900/20 focus-visible:ring-emerald-500')}
          aria-label={`Resume session ${session.name}`}
          title={canFireInPlace ? 'Resume — send the suggested next step' : 'Resume — open and continue'}
          onClick={() => onQuickResume(session.id, canFireInPlace ? suggestions[0] : undefined)}
        >
          <Play size={16} aria-hidden="true" />
        </button>
```

Add the caret menu (spec parity with the composer) when `suggestions.length >= 2 && !session.detached`. Add `const [menuOpen, setMenuOpen] = useState(false)` to the component and render, right after the play button:

```typescript
        {suggestions.length >= 2 && !session.detached && (
          <div className="relative flex items-center">
            <button
              type="button"
              aria-label={`More suggestions for ${session.name}`}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              className={cn(actionBtn, 'text-emerald-600 dark:text-emerald-400',
                'hover:bg-emerald-50 dark:hover:bg-emerald-900/20 focus-visible:ring-emerald-500')}
              onClick={() => setMenuOpen((o) => !o)}
            >
              <ChevronDown size={16} aria-hidden="true" />
            </button>
            {menuOpen && (
              <div
                role="menu"
                className={cn(
                  'absolute top-full right-0 mt-1 min-w-[12rem] max-w-xs z-10',
                  'rounded-md border border-gray-200 dark:border-gray-700',
                  'bg-white dark:bg-gray-800 shadow-lg py-1',
                )}
              >
                {suggestions.slice(1).map((s, i) => (
                  <button
                    key={i}
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); onQuickResume(session.id, s) }}
                    className={cn('block w-full truncate px-3 py-2 text-left text-sm',
                      'hover:bg-gray-100 dark:hover:bg-gray-700')}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
```

(`ChevronDown` is already imported in `SessionCard.tsx`. **Spec open question:** if the action row proves too crowded on mobile with Play + caret + Rename/Archive/Delete, collapse the caret into a long-press on the play button instead — note the decision in the PR.)

- [ ] **Step 4: Wire the Dashboard handler**

In `web/src/components/Dashboard.tsx`, add near the other session handlers (e.g. by `handleOpen` ~line 1172):

```typescript
  const sendPrompt = useSendPrompt()
  const handleQuickResume = useCallback((id: string, text?: string) => {
    if (text) {
      sendPrompt.mutate({ id, text })   // fire in place, stay on the list
    } else {
      handleOpen(id)                    // open the session; use its play button to generate + confirm
    }
  }, [sendPrompt, handleOpen])
```

Import `useSendPrompt` from `../api/hooks`. Pass the prop where `<SessionCard ... />` is rendered (~line 1489):

```typescript
          onQuickResume={handleQuickResume}
```

- [ ] **Step 5: Run tests + typecheck + lint to verify pass**

Run (in `web/`): `npx vitest run src/components/SessionCard.test.tsx && npx tsc --noEmit && npx eslint src/components/SessionCard.tsx src/components/Dashboard.tsx`
Expected: PASS; tsc clean; eslint clean.

> Per project memory (`dashboard-hooks-partial-mocks`): adding a hook import to `Dashboard.tsx` can break other Dashboard tests' partial `@/api/hooks` mocks (tsc/eslint won't catch it — only vitest). After this step, run the **full** web suite in Step 6 and, if Dashboard tests fail on a missing `useSendPrompt` mock, add `useSendPrompt: () => ({ mutate: vi.fn() })` to those tests' `@/api/hooks` mock factories.

- [ ] **Step 6: Run the full web suite**

Run (in `web/`): `npx vitest run && npx tsc --noEmit`
Expected: PASS. Fix any Dashboard mock fallout per the note above.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/SessionCard.tsx web/src/components/Dashboard.tsx web/src/components/SessionCard.test.tsx
git commit -m "feat(web): quick-resume play button on session cards"
```

---

## Final verification

- [ ] **Backend full suite:** `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/ -q` → all pass.
- [ ] **Web:** in `web/` → `npx vitest run && npx tsc --noEmit && npx eslint .` → all clean.
- [ ] **Manual smoke (optional, via the run skill):** open a session, let it go idle so a ghost suggestion appears, click the play button → suggestion sends. Empty the box with no suggestion, click play → spinner, then box prefills (not sent). On the dashboard, a live idle session with a suggestion shows a green play button that fires in place.

---

## Deferred work (explicitly NOT in this plan)

1. **Remote-daemon parity.** When `CCSWITCH_SESSIOND_SOCKET` is set, `app.state.sessions` is a `RemoteOrchestrator` (IPC), not the in-process adapter. The two new methods (`request_suggestions`, `latest_suggestions`) and the REST send-from-list (`send_prompt` already exists remotely) must be added to `server/sessions/remote.py` **and** the daemon-side IPC dispatch for the feature to work in remote mode. Track as a separate plan; this plan targets the in-process path (which is what dev + tests + the non-daemon deploy use). Until then, in remote mode the card play button degrades to "navigate" (latestSuggestions arrives empty) and the composer play falls back to the 8s quiet-fail.
2. **Auto-fire-on-open for the card's "no suggestion" case.** The agreed design (card rule C) said "none → open the session and *fill-and-confirm*." This plan navigates into the session and relies on the in-session play button for the fill-and-confirm (one extra tap). True auto-fire-on-open needs a routing intent (e.g. a `?resume=1` deep-link param consumed by `SessionView` to run `play()` once on mount), which depends on the deep-linkable-URLs routing. Add once the core ships if the extra tap proves annoying.
