# Generated-suggestion prefill for done/idle sessions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate and cache next-prompt suggestions for *detached* `done`/`idle` sessions so dashboard cards (Resume button + dropdown) and the focused Composer's existing ghost text light up for sessions the user has not opened.

**Architecture:** Make the core `SessionOrchestrator.latest_suggestions` async and fold detached-session generation into it. Because `latest_suggestions` already exists in the facade (`InProcessOrchestratorAdapter`, which `_maybe_await`s every call) and the daemon RPC dispatcher (`orchestrator_rpc._make_simple`, also `_maybe_await`), making the core async is transparent across in-process and remote/daemon modes — no new facade/remote/RPC entries. Generation reuses existing helpers and runs daemon-side where the suggester, records, and transcripts live. Frontend is unchanged (ghost text already renders `suggestions[0]`).

**Tech Stack:** Python 3 / FastAPI / asyncio backend; pytest. No frontend code changes.

## Global Constraints

- Sessions targeted: **`done` and `idle` only**. Skip all other statuses (`in_progress`, `waiting_input`, `waiting_question`, `ready_review`, `complete`, `needs_attention`, `limited`, `ended`).
- Suggestion-only: no `"continue"` fallback is introduced anywhere by this work (the card Resume button's pre-existing `"continue"` click-action fallback stays as-is and is NOT touched).
- In-memory cache only — no DB schema change.
- Never resume a driver to generate: generate from the on-disk transcript.
- Generation is best-effort and must never raise into `latest_suggestions` or the `/sessions` request path.
- Cache the result for a transcript signature **even when empty**, so the same transcript state is not re-generated on every poll (controls account-usage burn).
- Run backend lint gates before completion: `ruff`, `mypy`, `pytest` (per CLAUDE.md "Server CI lint gates").
- Tests run against the prebuilt env. From the worktree, use the deploy checkout's interpreter with `PYTHONPATH` set to the worktree `src` and `server` importable from repo root. Concretely, run pytest from the worktree root with the deploy venv:
  `PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/3a811f6f/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest …`
  (the `ccswitch-worktree-backend-pythonpath` memory documents the 2 known environmental failures unrelated to this work).

---

## File Structure

- **Modify** `server/sessions/orchestrator.py`
  - Add two instance fields in `__init__`: `self._detached_suggestions: dict[str, tuple[float, list[str]]]` and `self._suggest_inflight: set[str]`.
  - Replace the sync `latest_suggestions(self, session_id)` with an `async def` that keeps the live-driver fast path and adds the detached path.
  - Add `async def _generate_detached_suggestions(self, session_id, rec, path, signature)`.
- **Modify** `server/tests/test_suggestion_cache.py`
  - Update `test_orchestrator_latest_suggestions_reads_driver` to await the now-async method.
- **Create** `server/tests/test_detached_suggestions.py`
  - All new behavior tests for the detached path.

No other files change. The facade (`facade.py`), remote client (`remote.py`), daemon RPC (`orchestrator_rpc.py`), API (`api/sessions.py`), and all frontend files are unchanged.

---

## Task 1: Make `latest_suggestions` async and fix the existing sync test

This task only changes the method's async-ness and the live-driver path (behavior identical), and repairs the one synchronous caller. The detached generation logic is added in Task 2. Splitting here keeps the async migration independently reviewable from the new generation behavior.

**Files:**
- Modify: `server/sessions/orchestrator.py` (the `latest_suggestions` method, currently around lines 1086–1093)
- Modify: `server/tests/test_suggestion_cache.py:23-27` (`test_orchestrator_latest_suggestions_reads_driver`)

**Interfaces:**
- Produces: `async def latest_suggestions(self, session_id: str) -> list[str]` on `SessionOrchestrator`. Live-driver path returns `list(driver._last_suggestions or [])`. (Task 2 extends the detached branch.)

- [ ] **Step 1: Update the existing test to await the method**

In `server/tests/test_suggestion_cache.py`, replace the body of `test_orchestrator_latest_suggestions_reads_driver` so it drives the async method:

```python
def test_orchestrator_latest_suggestions_reads_driver():
    orch = SessionOrchestrator()
    orch._drivers["s1"] = types.SimpleNamespace(_last_suggestions=["A", "B"])
    assert asyncio.run(orch.latest_suggestions("s1")) == ["A", "B"]
    assert asyncio.run(orch.latest_suggestions("missing")) == []
```

(`asyncio` is already imported at the top of this test file.)

- [ ] **Step 2: Run the test to verify it FAILS against the still-sync method**

Run (from the worktree root):

```bash
PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/3a811f6f/src \
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest \
server/tests/test_suggestion_cache.py::test_orchestrator_latest_suggestions_reads_driver -v
```

Expected: FAIL — `asyncio.run()` raises `ValueError: a coroutine was expected` (the current method returns a plain list, not a coroutine). This confirms the test now requires an async method.

- [ ] **Step 3: Make the core method async (live-driver path only)**

In `server/sessions/orchestrator.py`, change the method signature from `def` to `async def` and keep the existing live-driver behavior. The `"missing"` / detached case still returns `[]` for now (Task 2 fills it in):

```python
    async def latest_suggestions(self, session_id: str) -> list[str]:
        """Latest cached next-prompt suggestions for a session.

        Live (attached) sessions read straight off the driver. Detached
        ``done``/``idle`` sessions generate from their on-disk transcript and
        cache the result (see _generate_detached_suggestions). Best-effort:
        never raises.
        """
        d = self._drivers.get(session_id)
        if d is not None:
            return list(getattr(d, "_last_suggestions", []) or [])
        return []
```

- [ ] **Step 4: Run the updated test and the full suggestion-cache file to verify PASS**

```bash
PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/3a811f6f/src \
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest \
server/tests/test_suggestion_cache.py -v
```

Expected: both tests PASS.

- [ ] **Step 5: Sanity-check no other caller broke**

Confirm the only callers go through an awaiting layer (facade `_maybe_await`, daemon `_make_simple` `_maybe_await`, API `await asyncio.gather(...)`):

```bash
grep -rn "latest_suggestions" server/ web/ | grep -v "def latest_suggestions" | grep -v test
```

Expected: matches are in `facade.py`, `remote.py`, `orchestrator_rpc.py`, and `api/sessions.py` only — all already async/awaited. No sync production caller.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_suggestion_cache.py
git commit -m "refactor(server): make orchestrator.latest_suggestions async

Transparent across facade/remote/daemon RPC (all _maybe_await). Prepares the
method to fold in detached-session generation. Updates the one synchronous
test caller to await.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Generate + cache suggestions for detached done/idle sessions

Adds the detached branch to `latest_suggestions` and the generator, plus the cache/dedup fields. This is the feature.

**Files:**
- Modify: `server/sessions/orchestrator.py` (`__init__` fields; `latest_suggestions` detached branch; new `_generate_detached_suggestions`)
- Create: `server/tests/test_detached_suggestions.py`

**Interfaces:**
- Consumes (all already present in `orchestrator.py`): `self._drivers`, `self._records` (`session_id -> SessionRecord` with `.status`, `.cwd`, `.account`), `self._suggester` (`SuggestionService | None`), `async self._resolve_transcript_path(session_id) -> str | None`, `compose` (imported from `ccswitch.configdir`), `replay.replay_events`, `suggestions.last_turn_context`.
- `SessionRecord.status` is one of: `idle / in_progress / waiting_input / ready_review / done / complete / needs_attention / limited / ended`.
- Produces: populated `self._detached_suggestions[session_id] = (mtime, items)`; `latest_suggestions` returns cached `items` for fresh detached `done`/`idle` sessions.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_detached_suggestions.py`. These use a fake suggester (`AsyncMock`) and a real temp transcript file; no live SDK. The transcript format is the JSONL the real `replay_events` parses — but to avoid coupling the test to transcript internals, we stub `_resolve_transcript_path` to point at a temp file and stub `replay_events` is NOT needed because we instead inject context via a fake; however the generator calls `replay_events(path)` then `last_turn_context(events)`. To keep the test honest end-to-end while avoiding transcript-format coupling, we monkeypatch `server.sessions.orchestrator.replay_events` and `server.sessions.orchestrator.last_turn_context` to deterministic fakes.

```python
import asyncio
import os
import types
import tempfile
from unittest.mock import AsyncMock

import pytest

import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator


def _orch_with_record(status="done", *, suggester_items=("Run tests", "Commit")):
    orch = SessionOrchestrator()
    orch._suggester = types.SimpleNamespace(
        generate=AsyncMock(return_value=list(suggester_items))
    )
    rec = types.SimpleNamespace(status=status, cwd="/x", account="acct")
    orch._records["s1"] = rec
    return orch, rec


def _temp_transcript():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.write(fd, b"{}\n")
    os.close(fd)
    return path


@pytest.fixture(autouse=True)
def _stub_replay(monkeypatch):
    # Generator calls replay_events(path) then last_turn_context(events).
    async def fake_replay(path):
        return ["evt"]
    monkeypatch.setattr(orch_mod, "replay_events", fake_replay, raising=False)
    monkeypatch.setattr(orch_mod, "last_turn_context", lambda events: ("hi", "done"), raising=False)
    monkeypatch.setattr(orch_mod, "compose", lambda account: "/cfg/" + account, raising=False)


async def _await_until_cached(orch, sid, tries=50):
    # latest_suggestions schedules a background task; poll until it lands.
    for _ in range(tries):
        res = await orch.latest_suggestions(sid)
        if res:
            return res
        await asyncio.sleep(0.01)
    return await orch.latest_suggestions(sid)


def test_detached_done_generates_and_caches():
    async def run():
        orch, _ = _orch_with_record("done")
        path = _temp_transcript()
        orch._resolve_transcript_path = AsyncMock(return_value=path)
        first = await orch.latest_suggestions("s1")     # kicks generation
        assert first == []                               # nothing cached yet
        got = await _await_until_cached(orch, "s1")
        assert got == ["Run tests", "Commit"]
        # Second call, same mtime → no second generate call.
        calls_before = orch._suggester.generate.call_count
        again = await orch.latest_suggestions("s1")
        assert again == ["Run tests", "Commit"]
        assert orch._suggester.generate.call_count == calls_before
    asyncio.run(run())


def test_detached_idle_status_is_eligible():
    async def run():
        orch, _ = _orch_with_record("idle")
        path = _temp_transcript()
        orch._resolve_transcript_path = AsyncMock(return_value=path)
        await orch.latest_suggestions("s1")
        got = await _await_until_cached(orch, "s1")
        assert got == ["Run tests", "Commit"]
    asyncio.run(run())


def test_non_done_idle_status_skipped():
    async def run():
        orch, _ = _orch_with_record("in_progress")
        orch._resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        res = await orch.latest_suggestions("s1")
        assert res == []
        assert orch._suggester.generate.call_count == 0
    asyncio.run(run())


def test_no_suggester_returns_empty():
    async def run():
        orch, _ = _orch_with_record("done")
        orch._suggester = None
        orch._resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        assert await orch.latest_suggestions("s1") == []
    asyncio.run(run())


def test_empty_result_is_cached_not_retried():
    async def run():
        orch, _ = _orch_with_record("done", suggester_items=())
        orch._resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        await orch.latest_suggestions("s1")          # kicks generation -> []
        for _ in range(50):
            await orch.latest_suggestions("s1")
            await asyncio.sleep(0.005)
        # Generated at most once for this transcript signature.
        assert orch._suggester.generate.call_count <= 1
    asyncio.run(run())


def test_signature_change_retriggers():
    async def run():
        orch, _ = _orch_with_record("done")
        path = _temp_transcript()
        orch._resolve_transcript_path = AsyncMock(return_value=path)
        await orch.latest_suggestions("s1")
        await _await_until_cached(orch, "s1")
        first_calls = orch._suggester.generate.call_count
        # Bump mtime to simulate a new turn on the transcript.
        st = os.stat(path)
        os.utime(path, (st.st_atime + 10, st.st_mtime + 10))
        await orch.latest_suggestions("s1")          # stale -> re-kick
        await _await_until_cached(orch, "s1")
        assert orch._suggester.generate.call_count > first_calls
    asyncio.run(run())


def test_inflight_dedup_single_generation():
    async def run():
        orch, _ = _orch_with_record("done")
        orch._resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        # Fire many concurrent awaits before the first generation completes.
        await asyncio.gather(*(orch.latest_suggestions("s1") for _ in range(8)))
        await _await_until_cached(orch, "s1")
        assert orch._suggester.generate.call_count == 1
    asyncio.run(run())


def test_live_driver_path_unchanged():
    async def run():
        orch, _ = _orch_with_record("done")
        orch._drivers["s1"] = types.SimpleNamespace(_last_suggestions=["X"])
        # Driver present → never touches the detached generator.
        assert await orch.latest_suggestions("s1") == ["X"]
        assert orch._suggester.generate.call_count == 0
    asyncio.run(run())
```

- [ ] **Step 2: Run the new tests to verify they FAIL**

```bash
PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/3a811f6f/src \
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest \
server/tests/test_detached_suggestions.py -v
```

Expected: failures — the detached branch returns `[]` and never calls the suggester (Task 1 left it stubbed), so `test_detached_done_generates_and_caches` and siblings fail. `test_non_done_idle_status_skipped`, `test_no_suggester_returns_empty`, and `test_live_driver_path_unchanged` may already pass — that's fine.

- [ ] **Step 3: Add the cache/dedup fields in `__init__`**

In `server/sessions/orchestrator.py` `__init__`, next to `self._drivers: dict[str, SessionDriver] = {}`, add:

```python
        # Detached-session next-prompt suggestions: session_id -> (transcript mtime, items).
        # Generated from the on-disk transcript without resuming the driver (see
        # latest_suggestions / _generate_detached_suggestions). Cached even when empty so
        # the same transcript state is not re-generated on every /sessions poll.
        self._detached_suggestions: dict[str, tuple[float, list[str]]] = {}
        self._suggest_inflight: set[str] = set()
```

- [ ] **Step 4: Ensure module-level imports for the generator**

At the top of `server/sessions/orchestrator.py`, confirm `import os` is present (it is — used elsewhere). Add module-level imports so they are monkeypatchable as `orchestrator.replay_events` / `orchestrator.last_turn_context` (the existing code imports `replay_events` lazily inside methods; add a top-level import alongside the other `server.sessions` imports):

```python
from server.sessions.replay import replay_events
from server.sessions.suggestions import last_turn_context
```

(`SuggestionService` and `compose` are already imported at module level.)

- [ ] **Step 5: Implement the detached branch + generator**

Replace the `latest_suggestions` body (from Task 1) with the full version, and add the generator method directly below it:

```python
    async def latest_suggestions(self, session_id: str) -> list[str]:
        """Latest cached next-prompt suggestions for a session.

        Live (attached) sessions read straight off the driver. Detached
        ``done``/``idle`` sessions generate from their on-disk transcript and
        cache the result keyed by transcript mtime (see
        _generate_detached_suggestions). Best-effort: never raises.
        """
        d = self._drivers.get(session_id)
        if d is not None:
            return list(getattr(d, "_last_suggestions", []) or [])

        rec = self._records.get(session_id)
        if rec is None or self._suggester is None:
            return []
        if rec.status not in ("done", "idle"):
            return []

        path = await self._resolve_transcript_path(session_id)
        if not path:
            return []
        try:
            signature = os.path.getmtime(path)
        except OSError:
            return []

        cached = self._detached_suggestions.get(session_id)
        if cached is not None and cached[0] == signature:
            return list(cached[1])

        if session_id not in self._suggest_inflight:
            self._suggest_inflight.add(session_id)
            asyncio.create_task(
                self._generate_detached_suggestions(session_id, rec, path, signature)
            )
        return list(cached[1]) if cached is not None else []

    async def _generate_detached_suggestions(self, session_id, rec, path, signature) -> None:
        """Generate suggestions for a detached session from its transcript (best-effort).

        Caches (signature, items) even when items is empty, so the same transcript
        state is not re-generated on the next poll. Always clears the in-flight flag.
        """
        try:
            events = await replay_events(path)
            user_text, assistant_text = last_turn_context(events)
            context = f"User: {user_text}\n\nAssistant: {assistant_text}"
            items = await self._suggester.generate(rec.cwd, str(compose(rec.account)), context)
            self._detached_suggestions[session_id] = (signature, list(items or []))
        except Exception:
            log.warning("detached suggestion generation failed id=%s", session_id, exc_info=True)
            self._detached_suggestions[session_id] = (signature, [])
        finally:
            self._suggest_inflight.discard(session_id)
```

- [ ] **Step 6: Run the new tests to verify they PASS**

```bash
PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/3a811f6f/src \
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest \
server/tests/test_detached_suggestions.py server/tests/test_suggestion_cache.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_detached_suggestions.py
git commit -m "feat(server): generate + cache suggestions for detached done/idle sessions

latest_suggestions now generates next-prompt suggestions for detached done/idle
sessions from their on-disk transcript (no driver resume), cached by transcript
mtime (empty results cached too) with in-flight dedup. Lights up dashboard card
Resume/dropdown and Composer ghost text for never-opened sessions.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Full backend gate (ruff + mypy + pytest) and finish

**Files:** none (verification + integration only).

- [ ] **Step 1: Lint — ruff**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/3a811f6f
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m ruff check server/sessions/orchestrator.py server/tests/test_detached_suggestions.py
```

Expected: no errors. Fix any reported issues (e.g. unused import, line length) and re-run.

- [ ] **Step 2: Types — mypy**

```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m mypy server/sessions/orchestrator.py
```

Expected: no new errors attributable to this change. If mypy flags the `rec`/`path`/`signature` params of `_generate_detached_suggestions`, add precise annotations: `(self, session_id: str, rec: SessionRecord, path: str, signature: float) -> None` (`SessionRecord` is already imported in this module).

- [ ] **Step 3: Run the targeted suggestion suite + a broad orchestrator-adjacent sweep**

```bash
PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/3a811f6f/src \
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest \
server/tests/test_detached_suggestions.py server/tests/test_suggestion_cache.py \
server/tests/test_suggestions.py server/tests/test_driver_suggestions.py -v
```

Expected: all PASS.

- [ ] **Step 4: Full backend suite (note known-environmental failures)**

```bash
PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/3a811f6f/src \
env -u CCSWITCH_ENABLE_XVFB \
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/ -q
```

Expected: green except the 2 pre-existing environmental failures documented in memory (`remote_mode_wiring`, x11) — confirm any failures are ONLY those, otherwise investigate. (`env -u CCSWITCH_ENABLE_XVFB` per the `ccswitch-tests-xvfb-env` memory.)

- [ ] **Step 5: Commit any lint/type fixups (if Steps 1–2 required changes)**

```bash
git add -A && git commit -m "chore(server): lint/type fixups for detached suggestions

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(Skip if nothing changed.)

---

## Self-Review

**Spec coverage:**
- Backend generation for detached done/idle (Part 1) → Task 2. ✓
- Async `latest_suggestions` folding, no new cross-layer wiring → Task 1 + Task 2. ✓
- mtime-signature cache, caches empty, in-flight dedup, done/idle gating → Task 2 Step 5 + tests. ✓
- Update the one sync test caller → Task 1 Step 1. ✓
- Composer unchanged (ghost text) → no task, by design (spec Part 2). ✓
- Cards unchanged (data-driven) → no task, by design (spec Part 3). ✓
- Lint gates (ruff/mypy/pytest) → Task 3. ✓

**Placeholder scan:** none — every code/test step shows full content.

**Type/name consistency:** `latest_suggestions` async signature is identical in Task 1 and Task 2; `_generate_detached_suggestions(self, session_id, rec, path, signature)` matches its call site; field names `_detached_suggestions` / `_suggest_inflight` are consistent across init, method, and tests; cache tuple shape `(mtime: float, items: list[str])` is consistent. Module-level `replay_events` / `last_turn_context` / `compose` names match the monkeypatch targets in the tests.
