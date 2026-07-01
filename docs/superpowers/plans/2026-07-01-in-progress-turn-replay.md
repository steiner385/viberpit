# In-progress Turn Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop live transcripts from truncating to a tail fragment when the event stream is re-subscribed mid-turn (reconnect, second viewer), by retaining and replaying the in-progress turn.

**Architecture:** The driver keeps a coalesced buffer of the current turn and fans its events out to per-subscriber queues; a new `events()` subscriber atomically snapshots that buffer then goes live. The JSONL replay is trimmed to exclude the in-progress turn (verified dup-free against the real binary), so history + in-progress-turn + live are disjoint. In daemon mode the same is achieved by subscribing the edge from the turn-start sequence in the daemon's replay ring.

**Tech Stack:** Python 3 (asyncio, FastAPI), pytest. No new dependencies.

## Global Constraints

- Governance: constitution `.specify/memory/constitution.md` — never fork the Claude binary; strict per-process isolation; verify against the real binary; no dead code / single source of truth.
- Add new model columns nullable only (N/A here — no schema changes).
- Tests: run with the main checkout's prebuilt venv, e.g. `/mnt/ssk-ssd/tony/GitHub/ccswitch/.venv/bin/python -m pytest` (a fresh worktree has no built env). If that path is absent, discover the repo's `.venv` python and use it.
- CI gates that must be green before done: `ruff`, `mypy`, `pytest` (server) and `vitest`/`tsc`/`eslint` (web — no web changes here, but don't break them).
- Coalesce text per block so the buffer is bounded by turn *content*, not delta *count*.
- Buffer only transcript-content events; never buffer control events (`Status`, `QueueUpdate`, `ContextUsage`, `Suggestions`, `Title`, `PermissionRequest`, `Question`, `Condensing`, `TurnFailure`) — those are re-emitted from authoritative state by `_emit_live_extras`.

**Design reference:** `docs/superpowers/specs/2026-07-01-in-progress-turn-replay-design.md`

---

### Task 1: Driver — in-progress turn buffer + broadcast fan-out

Replace the single destructive `_out` queue with a per-subscriber broadcast, and retain a coalesced buffer of the current turn that each new `events()` subscriber replays atomically before going live.

**Files:**
- Modify: `server/sessions/driver.py`
- Test: `server/tests/test_driver_replay_buffer.py` (create)

**Interfaces:**
- Consumes: `server/sessions/events.py` event dataclasses; `dataclasses.replace`.
- Produces:
  - `SessionDriver.turn_active -> bool` (property).
  - `SessionDriver.events()` — async generator: yields a snapshot of the current-turn buffer, then live events, per subscriber.
  - Internal: `SessionDriver._emit(e)` replaces every `await self._out.put(e)`; `SessionDriver._turn_buffer: list`; `SessionDriver._subscribers: set[asyncio.Queue]`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_driver_replay_buffer.py`:

```python
import asyncio
import dataclasses as dc
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import TextDelta, UserMessage, TurnEnd
from claude_agent_sdk import TextBlock
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


class _GatedSDK:
    """Streams two text deltas, then blocks on a gate before emitting the result,
    so a test can attach a second subscriber while the turn is still in progress."""
    def __init__(self, gate: asyncio.Event):
        self._gate = gate
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def query(self, prompt): pass
    async def interrupt(self): pass
    async def set_permission_mode(self, mode): pass
    async def receive_response(self):
        yield f.sys_init("sess-x")
        yield f.stream_start(uuid="t1")
        yield f.stream_text("Hello ", uuid="t1")
        yield f.stream_text("world", uuid="t1")
        await self._gate.wait()
        yield f.assistant(TextBlock(text="Hello world"), uuid="t1")
        yield f.result(session_id="sess-x", uuid="t1")


def test_late_subscriber_replays_in_progress_turn_then_goes_live():
    gate = asyncio.Event()

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return _GatedSDK(gate)

    async def run():
        d = SessionDriver("s", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        # Subscriber A drives the turn until 2 text deltas have streamed.
        a_seen = []
        gen_a = d.events()
        async def drive_a():
            async for e in gen_a:
                a_seen.append(e)
                if isinstance(e, TurnEnd):
                    break
        task_a = asyncio.create_task(drive_a())
        await d.send_prompt("hi")
        for _ in range(200):
            if sum(isinstance(e, TextDelta) for e in a_seen) >= 2:
                break
            await asyncio.sleep(0.01)
        assert d.turn_active is True

        # Subscriber B attaches mid-turn: its FIRST events must reconstruct the turn.
        gen_b = d.events()
        b_first = []
        for _ in range(6):
            b_first.append(await asyncio.wait_for(gen_b.__anext__(), timeout=1))
            if any(isinstance(e, TextDelta) and e.text == "Hello world" for e in b_first):
                break
        assert any(isinstance(e, UserMessage) for e in b_first)
        assert any(isinstance(e, TextDelta) and e.text == "Hello world" for e in b_first)

        # Now release the turn; B must receive the TurnEnd live (no duplicate text).
        gate.set()
        b_rest = []
        for _ in range(10):
            e = await asyncio.wait_for(gen_b.__anext__(), timeout=1)
            b_rest.append(e)
            if isinstance(e, TurnEnd):
                break
        b_text = "".join(e.text for e in (b_first + b_rest) if isinstance(e, TextDelta))
        assert b_text == "Hello world"   # coalesced snapshot + no live re-append
        await task_a
        await gen_b.aclose()
        await d.aclose()

    asyncio.run(run())


def test_buffer_cleared_at_settle_idle_subscriber_replays_nothing():
    msgs = [f.sys_init("s2"), f.stream_start(uuid="t1"),
            f.stream_text("done", uuid="t1"),
            f.assistant(TextBlock(text="done"), uuid="t1"), f.result(session_id="s2", uuid="t1")]

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient(msgs)

    async def run():
        d = SessionDriver("s2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        seen = []
        async for e in d.events():
            seen.append(e)
            if isinstance(e, TurnEnd):
                break
        assert d.turn_active is False
        assert d._turn_buffer == []
        await d.aclose()

    asyncio.run(run())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_driver_replay_buffer.py -x -q`
Expected: FAIL — `AttributeError: 'SessionDriver' object has no attribute 'turn_active'` (and no in-progress replay for subscriber B).

- [ ] **Step 3: Add the buffer, `_emit`, broadcast, and `turn_active` to the driver**

In `server/sessions/driver.py`:

Add `import dataclasses` near the top imports (after `import base64`):

```python
import base64
import dataclasses
```

Add the transcript-content tuple just below `_STREAM_DONE = object()`:

```python
_STREAM_DONE = object()

# Events that reconstruct a turn's transcript — the only ones retained in the
# in-progress-turn buffer and replayed to a late subscriber. Control/ephemeral
# events (Status, QueueUpdate, ContextUsage, Suggestions, Title, PermissionRequest,
# Question, Condensing, TurnFailure) are re-emitted from authoritative state by the
# WS attach path (_emit_live_extras); buffering them would double-emit.
_TURN_CONTENT = (ev.UserMessage, ev.TurnStart, ev.TurnModel, ev.SessionMeta,
                 ev.TextDelta, ev.ThinkingDelta, ev.ToolCall, ev.ToolResult,
                 ev.TurnEnd, ev.ErrorEvent)
```

In `__init__`, replace `self._out: asyncio.Queue = asyncio.Queue()` with:

```python
        # Broadcast fan-out: each events() subscriber gets its own queue. A single
        # asyncio.Queue is a WORK queue (each item to one getter) — fatal when
        # "attach" happens more than once per session (reconnect, 2nd viewer).
        self._subscribers: set[asyncio.Queue] = set()
        # Coalesced transcript events of the IN-PROGRESS turn, replayed to a late
        # subscriber so a mid-stream (re)attach shows the whole message, not the tail.
        self._turn_buffer: list = []
```

Add the emit/buffer helpers and `turn_active` property (place near `events()`, e.g. right above the existing `async def events`):

```python
    def _buffer_event(self, e) -> None:
        """Append e to the in-progress-turn buffer, coalescing consecutive same-block
        text/thinking deltas into one accumulated event (bounded by content, not count)."""
        if isinstance(e, (ev.TextDelta, ev.ThinkingDelta)) and self._turn_buffer:
            last = self._turn_buffer[-1]
            if (type(last) is type(e) and last.turn_id == e.turn_id
                    and last.src_uuid == e.src_uuid
                    and last.parent_tool_use_id == e.parent_tool_use_id):
                self._turn_buffer[-1] = dataclasses.replace(last, text=last.text + e.text)
                return
        self._turn_buffer.append(e)

    def _emit(self, e) -> None:
        """Fan an event out to every subscriber and (if transcript content) the buffer.
        Replaces the old single-queue `await self._out.put(e)`; put_nowait on unbounded
        subscriber queues never blocks, so this is synchronous."""
        if isinstance(e, _TURN_CONTENT):
            self._buffer_event(e)
        for q in self._subscribers:
            q.put_nowait(e)

    @property
    def turn_active(self) -> bool:
        return self._turn_active
```

Replace the body of `events()`:

```python
    async def events(self):
        # Snapshot the in-progress turn and register a fresh live queue ATOMICALLY —
        # no await between the two lines, so no event can slip into the gap (single
        # asyncio thread). Snapshot = the past; the queue = strictly the future.
        snapshot = list(self._turn_buffer)
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        try:
            for e in snapshot:
                yield e
            while True:
                item = await q.get()
                if item is _STREAM_DONE:
                    return
                yield item
        finally:
            self._subscribers.discard(q)
```

Reset the buffer at turn start — in `_start_turn`, next to `self._last_assistant_parts = []`:

```python
        self._last_assistant_parts = []
        self._turn_buffer = []
```

Clear the buffer when the turn settles into the JSONL — in `_drain`'s `finally`, the `else` (no queued prompts) branch, right after `self._turn_active = False`:

```python
            else:
                self._turn_active = False
                self._turn_buffer = []
                self._turns_completed += 1
```

Broadcast the stream-done sentinel — in `aclose()`, replace `await self._out.put(_STREAM_DONE)` with:

```python
        for q in self._subscribers:
            q.put_nowait(_STREAM_DONE)
```

Replace EVERY remaining `await self._out.put(X)` with `self._emit(X)`. There are ~18 call sites (lines ~191, 207, 236, 295, 398, 436, 442, 459, 501, 553, 568, 582, 610, 643, 663, 708, 709, 748). Verify none remain:

```bash
grep -n "self._out" server/sessions/driver.py
```
Expected after edits: only the two `self._subscribers` loops and `_turn_buffer`/`_emit` references — NO `self._out` at all. Remove the now-unused `_out` attribute (already replaced in `__init__`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_driver_replay_buffer.py server/tests/test_driver.py -q`
Expected: PASS (new tests pass; existing driver tests still pass — they consume `d.events()`, whose contract is unchanged for a single subscriber).

- [ ] **Step 5: Lint + type-check the driver**

Run: `.venv/bin/python -m ruff check server/sessions/driver.py && .venv/bin/python -m mypy server/sessions/driver.py`
Expected: clean (fix any issues, e.g. add `# type: ignore` only if a real SDK typing gap, otherwise correct the code).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_replay_buffer.py
git commit -m "fix(sessions): retain + broadcast in-progress turn so reattach replays it

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Trim the in-progress turn from the JSONL replay

Add a `drop_incomplete_turn` flag to `replay_window` that drops everything from the last real `UserMessage` onward, so the JSONL history and the driver buffer cover disjoint turns.

**Files:**
- Modify: `server/sessions/replay.py`
- Test: `server/tests/test_replay_trim.py` (create)

**Interfaces:**
- Consumes: `server/sessions/events.py` (`ev.UserMessage`).
- Produces: `replay_window(jsonl_path, *, before=None, limit=20, drop_incomplete_turn=False)` and `_window(path, before, limit, drop_incomplete_turn=False)`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_replay_trim.py`:

```python
import json
import asyncio
from server.sessions.replay import replay_window
from server.sessions import events as ev


def _write(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def _turn(uuid_u, uuid_a, text):
    return [
        {"type": "user", "uuid": uuid_u, "message": {"role": "user", "content": text}},
        {"type": "assistant", "uuid": uuid_a,
         "message": {"role": "assistant", "model": "claude-x",
                     "content": [{"type": "text", "text": text + "-reply"}]}},
    ]


def test_drop_incomplete_turn_removes_trailing_user_onward(tmp_path):
    rows = _turn("u1", "a1", "first") + _turn("u2", "a2", "second")
    # trailing in-progress turn: real user + one completed assistant text block
    rows += [{"type": "user", "uuid": "u3", "message": {"role": "user", "content": "third"}},
             {"type": "assistant", "uuid": "a3",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "text", "text": "partial"}]}}]
    path = _write(tmp_path, rows)

    kept = asyncio.run(replay_window(path, drop_incomplete_turn=True)).events
    users = [e for e in kept if isinstance(e, ev.UserMessage)]
    assert [u.text for u in users] == ["first", "second"]     # "third" dropped
    assert not any(getattr(e, "text", None) == "partial" for e in kept)

    full = asyncio.run(replay_window(path, drop_incomplete_turn=False)).events
    assert [u.text for u in full if isinstance(u, ev.UserMessage)] == ["first", "second", "third"]


def test_drop_incomplete_turn_ignores_tool_result_user_lines(tmp_path):
    rows = _turn("u1", "a1", "first")
    # in-progress turn: real user, assistant tool_use, then a tool_result "user" line
    rows += [{"type": "user", "uuid": "u2", "message": {"role": "user", "content": "second"}},
             {"type": "assistant", "uuid": "a2",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "tool_use", "id": "tool1", "name": "Bash", "input": {}}]}},
             {"type": "user", "uuid": "ur",
              "message": {"role": "user",
                          "content": [{"type": "tool_result", "tool_use_id": "tool1", "content": "ok"}]}}]
    path = _write(tmp_path, rows)
    kept = asyncio.run(replay_window(path, drop_incomplete_turn=True)).events
    # The trim boundary is the last REAL user ("second"), not the tool_result line.
    assert [u.text for u in kept if isinstance(u, ev.UserMessage)] == ["first"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_replay_trim.py -x -q`
Expected: FAIL — `TypeError: replay_window() got an unexpected keyword argument 'drop_incomplete_turn'`.

- [ ] **Step 3: Implement the trim**

In `server/sessions/replay.py`, change `_window` to accept and apply the flag:

```python
def _window(path: str, before: int | None, limit: int, drop_incomplete_turn: bool = False) -> ReplayWindow:
    out, meta_event = _build_events(path)
    if drop_incomplete_turn:
        # Drop the trailing in-progress logical turn: everything from the last real
        # UserMessage onward. The live driver buffer / daemon ring owns that turn, and
        # its blocks are flushed to disk mid-stream, so keeping them here duplicates.
        last_user = max((i for i, e in enumerate(out) if isinstance(e, ev.UserMessage)),
                        default=None)
        if last_user is not None:
            out = out[:last_user]
    starts = [i for i, e in enumerate(out) if isinstance(e, ev.TurnStart)]
    ends   = [i for i, e in enumerate(out) if isinstance(e, ev.TurnEnd)]
```

(Leave the rest of `_window` unchanged.) Then thread the flag through the async wrapper:

```python
async def replay_window(jsonl_path: str, *, before: int | None = None, limit: int = 20,
                        drop_incomplete_turn: bool = False) -> ReplayWindow:
    return await asyncio.to_thread(_window, jsonl_path, before, limit, drop_incomplete_turn)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_replay_trim.py server/tests/test_ipc_replay.py -q`
Expected: PASS (new tests pass; existing replay tests unaffected — the flag defaults False).

- [ ] **Step 5: Lint + type-check**

Run: `.venv/bin/python -m ruff check server/sessions/replay.py && .venv/bin/python -m mypy server/sessions/replay.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/replay.py server/tests/test_replay_trim.py
git commit -m "feat(replay): drop_incomplete_turn trims the in-progress turn from history

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: ws.py attach seam — trim JSONL by `driver.turn_active`

Make the in-process attach path trim the in-progress turn from the JSONL replay when a turn is active, so `events()`' buffer replay is the sole source for that turn.

**Files:**
- Modify: `server/bridge/ws.py`
- Test: `server/tests/test_ws_inprogress_replay.py` (create)

**Interfaces:**
- Consumes: `SessionDriver.turn_active`; `replay_window(..., drop_incomplete_turn=...)`.
- Produces: `_replay_and_complete(session_id, drop_incomplete=False)` (new keyword); attach passes `drop_incomplete=driver.turn_active`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ws_inprogress_replay.py`. This is a focused unit test of `_replay_and_complete`'s trimming via a fake orchestrator; it asserts the replayed batch excludes the in-progress turn when the driver reports a turn active.

```python
import json
import asyncio
import pytest
from server.bridge import ws as wsmod


class _FakeDriver:
    def __init__(self, active): self._active = active
    @property
    def turn_active(self): return self._active
    def current_status(self): return "in_progress" if self._active else "idle"
    def queued(self): return []


def _write_transcript(tmp_path):
    def turn(u, a, text):
        return [
            {"type": "user", "uuid": u, "message": {"role": "user", "content": text}},
            {"type": "assistant", "uuid": a,
             "message": {"role": "assistant", "model": "m",
                         "content": [{"type": "text", "text": text + "-r"}]}},
        ]
    rows = turn("u1", "a1", "first")
    rows += [{"type": "user", "uuid": "u2", "message": {"role": "user", "content": "second"}},
             {"type": "assistant", "uuid": "a2",
              "message": {"role": "assistant", "model": "m",
                          "content": [{"type": "text", "text": "partial"}]}}]
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def test_replay_trims_in_progress_turn_when_active(tmp_path, monkeypatch):
    path = _write_transcript(tmp_path)

    # Drive replay_window through the real trim, but capture what the seam requests.
    from server.sessions import replay as replaymod
    calls = {}
    real = replaymod.replay_window
    async def spy(p, **kw):
        calls.update(kw)
        return await real(p, **kw)
    monkeypatch.setattr(wsmod, "replay_window", spy)

    # Minimal orchestrator stub exposing transcript_path.
    class Orch:
        async def transcript_path(self, sid): return path
    sent = []
    async def send(msg): sent.append(msg)

    # Exercise the extracted replay helper directly.
    async def run():
        # Build the closure the way ws.py does: bind orchestrator + _send.
        await wsmod._run_replay_and_complete(Orch(), send, "s1", drop_incomplete=True)  # type: ignore[attr-defined]
    asyncio.run(run())

    assert calls.get("drop_incomplete_turn") is True
    batch = next(m for m in sent if m.get("type") == "batch")
    users = [e for e in batch["events"] if e.get("type") == "user"]
    assert [u["text"] for u in users] == ["first"]   # "second" (in-progress) trimmed
```

> Note: this test calls a small module-level helper `_run_replay_and_complete(orchestrator, send, session_id, drop_incomplete)`. Extract the existing closure body into that helper in Step 3 so it is unit-testable without a live WebSocket; the in-closure `_replay_and_complete` then delegates to it.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_ws_inprogress_replay.py -x -q`
Expected: FAIL — `AttributeError: module 'server.bridge.ws' has no attribute '_run_replay_and_complete'`.

- [ ] **Step 3: Extract the helper and thread the flag**

In `server/bridge/ws.py`, add a module-level helper (above `build_ws_router`) that contains the replay logic, parameterized by `orchestrator`, `send`, and `drop_incomplete`:

```python
async def _run_replay_and_complete(orchestrator, send, session_id: str,
                                   drop_incomplete: bool = False) -> list:
    """Replay the transcript (trimmed of the in-progress turn when drop_incomplete) as
    one batch, then replayComplete. Returns the replayed events. See build_ws_router's
    _replay_and_complete for the rationale (reset-leading batch, always-fire complete)."""
    events: list = []
    earliest_turn = 0
    total_turns = 0
    try:
        path = await orchestrator.transcript_path(session_id)
        if path:
            window = await replay_window(path, drop_incomplete_turn=drop_incomplete)
            events = window.events
            earliest_turn = window.earliest_turn
            total_turns = window.total_turns
        if events:
            redacted_replay = [{"type": "replayReset"}]
            redacted_replay += [redact_event(e.to_wire()) for e in events]
            await send({"type": "batch", "events": redacted_replay})
    except Exception:
        log.warning("ws.attach: transcript replay failed for %s", session_id, exc_info=True)
    await send({"type": "replayComplete", "earliestTurn": earliest_turn, "totalTurns": total_turns})
    return events
```

Change the in-closure `_replay_and_complete` to delegate:

```python
        async def _replay_and_complete(session_id: str, drop_incomplete: bool = False) -> list:
            return await _run_replay_and_complete(orchestrator, _send, session_id, drop_incomplete)
```

In `_handle_attach`, the LIVE branch, pass the flag from the driver:

```python
            driver = await _resume_driver_or_error(session_id)
            if driver is None:
                return
            active = getattr(driver, "turn_active", False)
            events = await _replay_and_complete(session_id, drop_incomplete=active)
            await _emit_live_extras(session_id, driver, events)
            _start_live_stream(session_id, driver)
```

(The detached branch keeps `await _replay_and_complete(session_id)` — no active turn, so no trim.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_ws_inprogress_replay.py server/tests/test_initial_prompt_visible.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + type-check**

Run: `.venv/bin/python -m ruff check server/bridge/ws.py && .venv/bin/python -m mypy server/bridge/ws.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws_inprogress_replay.py
git commit -m "fix(ws): trim in-progress turn from replay so events() owns it (no dup)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Daemon — turn-start sequence tracking + `ensure_driver` resume anchor

Make the daemon replay ring able to serve the in-progress turn: track the turn's start sequence and open/closed state, expose a `resume_since`, and return it (plus `turn_active`) from `ensure_driver`. Bump the ring so a normal turn fits.

**Files:**
- Modify: `server/ipc/daemon.py`
- Modify: `server/ipc/orchestrator_rpc.py`
- Test: `server/tests/test_daemon_turn_resume.py` (create)

**Interfaces:**
- Consumes: existing `app.state.publish`, `app.state.head_seq`.
- Produces: `app.state.resume_since(stream) -> int | None`; `app.state.turn_open_for(stream) -> bool`; `ensure_driver` RPC result gains `"resume_since"` and `"turn_active"`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_daemon_turn_resume.py`:

```python
import asyncio
from server.ipc.daemon import create_app


def test_publish_tracks_turn_start_and_open_state():
    app = create_app()

    async def run():
        s = "sess"
        await app.state.publish(s, {"type": "user", "text": "hi"})       # seq 0
        await app.state.publish(s, {"type": "textDelta", "text": "a"})   # seq 1
        await app.state.publish(s, {"type": "textDelta", "text": "b"})   # seq 2
        assert app.state.turn_open_for(s) is True
        # mid-turn: resume from just before the user message → replays the whole turn
        assert app.state.resume_since(s) == -1   # turn_start_seq(0) - 1
        await app.state.publish(s, {"type": "turnEnd", "turnId": "t"})   # seq 3
        assert app.state.turn_open_for(s) is False
        assert app.state.resume_since(s) == app.state.head_seq(s)        # from-now once closed

    asyncio.run(run())


def test_status_terminal_closes_turn_when_no_turnend():
    app = create_app()

    async def run():
        s = "sess2"
        await app.state.publish(s, {"type": "user", "text": "hi"})       # seq 0 -> open
        await app.state.publish(s, {"type": "textDelta", "text": "x"})   # seq 1
        await app.state.publish(s, {"type": "status", "state": "needs_attention"})
        assert app.state.turn_open_for(s) is False

    asyncio.run(run())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_daemon_turn_resume.py -x -q`
Expected: FAIL — `AttributeError: 'State' object has no attribute 'turn_open_for'`.

- [ ] **Step 3: Implement daemon tracking + resume anchor**

In `server/ipc/daemon.py`, near the other `app.state.*` buffer setup (after `app.state.seqs = {}`), add:

```python
    # Turn-boundary tracking for in-progress-turn replay: the seq of the current
    # turn's user message, and whether a turn is open. A reconnecting edge resumes
    # from turn_start_seq so the whole in-progress turn replays from the ring.
    app.state.turn_start_seqs = {}   # stream -> seq of the last `user` event
    app.state.turn_open = {}         # stream -> bool
    _TERMINAL_STATES = {"done", "complete", "needs_attention", "idle"}
```

In `publish`, after `buf.append((seq, event))`, add the tracking:

```python
        buf.append((seq, event))
        etype = event.get("type")
        if etype == "user":
            app.state.turn_start_seqs[stream] = seq
            app.state.turn_open[stream] = True
        elif etype == "turnEnd":
            app.state.turn_open[stream] = False
        elif etype == "status" and event.get("state") in _TERMINAL_STATES:
            app.state.turn_open[stream] = False
```

Add accessors next to `head_seq`:

```python
    def turn_open_for(stream: str) -> bool:
        return bool(app.state.turn_open.get(stream))

    app.state.turn_open_for = turn_open_for

    def resume_since(stream: str) -> int | None:
        # While a turn is open, resume from just before its user message so the whole
        # in-progress turn replays from the ring; otherwise from-now (disk has the rest).
        if app.state.turn_open.get(stream):
            return app.state.turn_start_seqs.get(stream, 0) - 1
        return head_seq(stream)

    app.state.resume_since = resume_since
```

Also extend `evict` to clear the new maps (so a torn-down stream doesn't leak stale turn state):

```python
    def evict(stream: str) -> None:
        app.state.buffers.pop(stream, None)
        app.state.seqs.pop(stream, None)
        app.state.stats.pop(stream, None)
        app.state.turn_start_seqs.pop(stream, None)
        app.state.turn_open.pop(stream, None)
```

Bump the default ring size so a normal turn fits (a long reply is a handful of coalesced blocks, but tool-heavy turns emit many events):

```python
    _maxlen = int(os.environ.get("CCSWITCH_IPC_REPLAY_BUFFER", "2048"))
```

In `server/ipc/orchestrator_rpc.py`, extend the `ensure_driver` return to carry the resume anchor and turn state:

```python
        return {"found": True, "status": driver.current_status(), "queued": driver.queued(),
                "head_seq": app.state.head_seq(session_id),
                "resume_since": app.state.resume_since(session_id),
                "turn_active": app.state.turn_open_for(session_id)}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_daemon_turn_resume.py server/tests/test_ipc_replay.py server/tests/test_ipc_backpressure.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + type-check**

Run: `.venv/bin/python -m ruff check server/ipc/daemon.py server/ipc/orchestrator_rpc.py && .venv/bin/python -m mypy server/ipc/daemon.py server/ipc/orchestrator_rpc.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/ipc/daemon.py server/ipc/orchestrator_rpc.py server/tests/test_daemon_turn_resume.py
git commit -m "feat(ipc): daemon tracks turn-start seq so reconnect replays the in-progress turn

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: RemoteDriver — resume from turn start + expose `turn_active`

Make the edge-side remote driver subscribe from the daemon's `resume_since` (so it replays the in-progress turn) and expose `turn_active` so the ws seam trims the JSONL identically to in-process.

**Files:**
- Modify: `server/sessions/remote.py`
- Test: `server/tests/test_remote_driver.py` (add cases)

**Interfaces:**
- Consumes: `ensure_driver` RPC result fields `resume_since`, `turn_active`.
- Produces: `RemoteDriver(..., resume_since=None, turn_active=False)`; `RemoteDriver.turn_active` attribute; `RemoteDriver.events()` subscribes `since=resume_since` (fallback to `head_seq`).

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_remote_driver.py`:

```python
def test_remote_driver_subscribes_from_resume_since_and_exposes_turn_active():
    import asyncio
    from server.sessions.remote import RemoteDriver

    captured = {}
    class FakeClient:
        def subscribe(self, session_id, since=None):
            captured["since"] = since
            async def gen():
                if False:
                    yield None
            return gen()

    async def run():
        d = RemoteDriver(FakeClient(), "s", status="in_progress", queued=[],
                         head_seq=10, resume_since=3, turn_active=True)
        assert d.turn_active is True
        async for _ in d.events():
            pass
    asyncio.run(run())
    assert captured["since"] == 3   # resume_since, not head_seq
```

Also add a case asserting the RemoteOrchestrator passes the fields through:

```python
def test_remote_orchestrator_threads_resume_fields():
    import asyncio
    from server.sessions.remote import RemoteOrchestrator

    class FakeClient:
        async def call(self, method, **kw):
            assert method == "ensure_driver"
            return {"found": True, "status": "in_progress", "queued": [],
                    "head_seq": 10, "resume_since": 3, "turn_active": True}
        def subscribe(self, *a, **k):
            async def gen():
                if False:
                    yield None
            return gen()

    async def run():
        orch = RemoteOrchestrator(FakeClient())
        d = await orch.ensure_driver("s", "ident")
        assert d.turn_active is True
        assert d._resume_since == 3
    asyncio.run(run())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_remote_driver.py -x -q -k "resume_since or turn_active"`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'resume_since'`.

- [ ] **Step 3: Implement RemoteDriver changes**

In `server/sessions/remote.py`, extend `RemoteDriver.__init__`:

```python
    def __init__(self, client, session_id: str, status="idle", queued=None, head_seq=None,
                 resume_since=None, turn_active=False):
        self._client = client
        self._session_id = session_id
        self._status = status
        self._queued = queued or []
        self._head_seq = head_seq
        # Where to resume the daemon topic: while a turn is open this is just before its
        # user message, so the whole in-progress turn replays from the ring; else from-now.
        self._resume_since = resume_since if resume_since is not None else head_seq
        self.turn_active = bool(turn_active)
        self._bg_tasks: set[asyncio.Task] = set()
```

Change `events()` to use the resume anchor:

```python
    async def events(self):
        async for wire in self._client.subscribe(self._session_id, since=self._resume_since):
            yield _WireEvent(wire)
```

Update `RemoteOrchestrator.ensure_driver` to thread the new fields (find the `return RemoteDriver(...)` line):

```python
        return RemoteDriver(self._client, session_id, status=r["status"], queued=r["queued"],
                            head_seq=r.get("head_seq"), resume_since=r.get("resume_since"),
                            turn_active=r.get("turn_active", False))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_remote_driver.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + type-check**

Run: `.venv/bin/python -m ruff check server/sessions/remote.py && .venv/bin/python -m mypy server/sessions/remote.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/remote.py server/tests/test_remote_driver.py
git commit -m "fix(remote): resume from turn-start seq + expose turn_active (reattach replay)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Full-suite verification + PR

**Files:** none (verification + integration).

- [ ] **Step 1: Run the full server suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/ -q`
Expected: all pass. Investigate any failure before proceeding (do not skip).

- [ ] **Step 2: Full lint + type-check gate (matches CI `lint-and-test`)**

Run: `.venv/bin/python -m ruff check server/ && .venv/bin/python -m mypy server/`
Expected: clean.

- [ ] **Step 3: Confirm no stray `self._out` and no dead code**

Run: `grep -rn "self._out\b" server/sessions/driver.py; grep -rn "live_turn_events" server/`
Expected: no `self._out` matches; no `live_turn_events` references (the buffer replay lives in `events()`, per the design refinement).

- [ ] **Step 4: Push branch and open PR**

```bash
git push -u origin fix/in-progress-turn-replay
gh pr create --title "fix: replay in-progress turn so live transcripts don't truncate on reattach" \
  --body "$(cat <<'EOF'
## Summary
Live transcripts sometimes rendered only the tail of an assistant message (e.g. `ate).`)
whenever the event stream was re-subscribed mid-turn — a WebSocket reconnect or a second
viewer. Root cause: the in-progress turn lived only in an ephemeral, single-consumer queue
(in-process) / a from-now topic subscription (daemon), and the JSONL on disk holds only
completed blocks. On re-subscribe the already-streamed portion was unrecoverable.

## Fix
- Driver retains a coalesced buffer of the in-progress turn and broadcasts events to
  per-subscriber queues; a new `events()` subscriber atomically snapshots the buffer then
  goes live (in-process reconnect + multi-viewer).
- `replay_window(drop_incomplete_turn=…)` trims the in-progress turn from the JSONL replay so
  history + buffer + live are disjoint (verified against the real binary: the user line and
  completed blocks flush before the turn ends, so the trim boundary is unambiguous).
- Daemon tracks the turn-start sequence + open/closed state and returns a `resume_since`;
  `RemoteDriver` subscribes from it so the daemon ring replays the whole in-progress turn.

Design + spike: `docs/superpowers/specs/2026-07-01-in-progress-turn-replay-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

Expected: PR opens non-draft with auto-merge armed; CI (`lint-and-test`, `frontend`) runs.

## Self-Review notes

- **Spec coverage:** Layer 1 (buffer + broadcast) → Task 1; JSONL trim → Task 2 + Task 3 seam; Layer 2 daemon → Task 4; RemoteDriver → Task 5; ring bump → Task 4; testing → per-task + Task 6.
- **Type consistency:** `turn_active` (property on driver, attribute on RemoteDriver) read via `getattr(driver, "turn_active", False)` in ws.py; `resume_since` int|None end-to-end; `drop_incomplete_turn` bool end-to-end.
- **Known follow-ups (documented, not in scope):** sub-second settle-window on the JSONL flush; remote turns longer than the ring fall back to disk resync.
