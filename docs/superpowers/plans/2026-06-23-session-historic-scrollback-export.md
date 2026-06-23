# Session Historic Scrollback + Transcript Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users scroll back to the very first turn of a session (lazy turn-ordinal paging over the SDK's on-disk JSONL) and export a session transcript as Markdown or raw JSONL.

**Architecture:** The transcript UI rebuilds `Turn`/`Block` trees from typed wire events; the server reconstructs those events by parsing `<config_dir>/projects/<project>/<claude_session_id>.jsonl`. Replay is hardcoded to the last 20 turns. We parameterize parsing into a turn-ordinal *window*, page older windows over the existing REST transcript endpoint, prepend them in the reducer by reusing the same reducer to fold the older events, and add two export download endpoints. No new data is persisted.

**Tech Stack:** Python (FastAPI, pytest) backend · React + TypeScript (vitest) frontend.

## Global Constraints

- Constitution compliance: never patch the Claude binary; account = `CLAUDE_CONFIG_DIR`; verify against the real artifact; security-first; TDD + quality gates; single source of truth / no dead code.
- New SQLAlchemy columns must be nullable — **N/A here, no schema changes**.
- Server tests run from the prebuilt env: `<main-checkout>/.venv/bin/python -m pytest server/tests/...`. Pin `CCSWITCH_TAILNET_ALLOWLIST` per-test or run with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`.
- Web tests run in `web/`: `vitest run`, `tsc --noEmit`, `eslint`. Use the local `.bin`, not `npx`; `node_modules` is symlinked from the deploy worktree.
- Redacted wire events for all browser-facing event paths (`redact_event(e.to_wire())`); the raw JSONL export is the deliberate exception (truly unredacted).
- Frequent commits: one commit per task.

## Reference: existing signatures (do not redefine — consume as-is)

- `server/sessions/replay.py`: `_parse(path: str, max_turns: int) -> list`; `async replay_events(jsonl_path, *, max_turns=20) -> list`. Turn boundary = `ev.TurnStart`. `ev.SessionMeta` is held aside and prepended.
- `server/sessions/orchestrator.py`: `transcript_path(session_id) -> str | None` (line ~1084); `async transcript_events(session_id) -> list` (line ~1593).
- `server/api/sessions.py`: `GET /{session_id}/transcript` (line ~488) returns `{"events": [...]}`, redacted. Auth dep: `current_identity`. 404 shape: `detail={"code": "not_found", "message": ...}`.
- `server/bridge/ws.py`: `_replay_and_complete(session_id)` (line ~169) sends one `{"type":"batch","events":[{"type":"replayReset"},...]}` then `await _send({"type":"replayComplete"})` (line 195). Also sends bare `replayComplete` at lines 219/240/246 (error paths).
- `web/src/session/reducer.ts`: `transcriptReducer(state, ev)` (line 252); `initialTranscript` (line 69); `TranscriptState` (line 54); `Turn` (line 22); `LocalAction` (line 91). Block-event routing checks `parentToolUseId` at top of reducer.
- `web/src/session/events.ts`: `EvReplayComplete = { type: 'replayComplete' }`; `SessionEvent` union; `SESSION_EVENT_TYPES` set.
- `web/src/api/client.ts`: `api.get<T>(path)`, `api.post`, etc. `fetch` is `/api${path}`.
- `web/src/session/useSessionRuntime.ts`: `useReducer(transcriptReducer, initialTranscript)` (line 82); returns `SessionRuntime` (interface line 26). `dispatch` available.
- `web/src/components/session/SessionBody.tsx`: renders `<TranscriptView ... />` at line 158 from `rt` (a `SessionRuntime`).
- `web/src/components/session/TranscriptView.tsx`: scroll container `containerRef`, `handleScroll`, auto-scroll effect on `[turns, rotations]`.

---

### Task 1: Backend — windowed replay (`replay_window`)

**Files:**
- Modify: `server/sessions/replay.py`
- Test: `server/tests/test_replay_window.py` (create)

**Interfaces:**
- Produces: `class ReplayWindow` with attributes `events: list`, `earliest_turn: int`, `total_turns: int`. `async replay_window(jsonl_path: str, *, before: int | None = None, limit: int = 20) -> ReplayWindow`. Turns indexed `0..total_turns-1` from session start. `before=None` ⇒ last `limit` turns. `before=K` ⇒ turns with ordinals in `[max(0,K-limit), K)`. `earliest_turn` = lowest ordinal in the returned window (0 when none). `replay_events` keeps its current signature, reframed as a wrapper returning `.events`.
- Consumes: `from . import events as ev`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_replay_window.py`:

```python
from __future__ import annotations
import json
import pytest
from server.sessions.replay import replay_window, replay_events


def _write_transcript(tmp_path, n_turns: int):
    """Write a JSONL transcript with n_turns user+assistant turn pairs."""
    p = tmp_path / "t.jsonl"
    lines = []
    for i in range(n_turns):
        lines.append({"type": "user", "uuid": f"u{i}",
                      "message": {"content": f"q{i}"}})
        lines.append({"type": "assistant", "uuid": f"a{i}",
                      "message": {"model": "claude-x",
                                  "content": [{"type": "text", "text": f"r{i}"}],
                                  "usage": {"input_tokens": 1}}})
    p.write_text("\n".join(json.dumps(o) for o in lines))
    return str(p)


def _turn_ids(window):
    from server.sessions import events as ev
    return [e.turn_id for e in window.events if isinstance(e, ev.TurnStart)]


@pytest.mark.asyncio
async def test_window_default_returns_last_n(tmp_path):
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, limit=20)
    assert w.total_turns == 50
    assert w.earliest_turn == 30           # turns 30..49
    assert _turn_ids(w) == [f"a{i}" for i in range(30, 50)]


@pytest.mark.asyncio
async def test_window_before_cursor(tmp_path):
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, before=30, limit=20)
    assert w.earliest_turn == 10           # turns 10..29
    assert _turn_ids(w) == [f"a{i}" for i in range(10, 30)]


@pytest.mark.asyncio
async def test_window_reaches_start(tmp_path):
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, before=10, limit=20)
    assert w.earliest_turn == 0            # clamped at start: turns 0..9
    assert _turn_ids(w) == [f"a{i}" for i in range(0, 10)]


@pytest.mark.asyncio
async def test_window_before_past_end_is_clamped(tmp_path):
    path = _write_transcript(tmp_path, 5)
    w = await replay_window(path, before=999, limit=20)
    assert w.total_turns == 5
    assert w.earliest_turn == 0
    assert _turn_ids(w) == [f"a{i}" for i in range(0, 5)]


@pytest.mark.asyncio
async def test_window_empty_transcript(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    w = await replay_window(str(p))
    assert w.total_turns == 0
    assert w.earliest_turn == 0
    assert w.events == []


@pytest.mark.asyncio
async def test_session_meta_present_in_every_window(tmp_path):
    from server.sessions import events as ev
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, before=20, limit=10)
    assert any(isinstance(e, ev.SessionMeta) for e in w.events)


@pytest.mark.asyncio
async def test_replay_events_wrapper_unchanged(tmp_path):
    path = _write_transcript(tmp_path, 30)
    events = await replay_events(path, max_turns=20)
    from server.sessions import events as ev
    starts = [e for e in events if isinstance(e, ev.TurnStart)]
    assert len(starts) == 20
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_replay_window.py -v`
Expected: FAIL with `ImportError: cannot import name 'replay_window'`.

- [ ] **Step 3: Implement `replay_window`**

In `server/sessions/replay.py`, refactor so `_parse` separates event-building from trimming. Replace the trim tail (lines ~92-100) and add the window API:

```python
from __future__ import annotations
import json, asyncio
from dataclasses import dataclass, field
from . import events as ev


@dataclass
class ReplayWindow:
    events: list = field(default_factory=list)
    earliest_turn: int = 0
    total_turns: int = 0


def _build_events(path: str) -> tuple[list, object | None]:
    """Parse the full JSONL into (events, meta_event). No trimming."""
    lines = []
    with open(path, errors="ignore") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    out: list = []
    meta_event = None
    for o in lines:
        if o.get("isSidechain"):
            continue
        typ = o.get("type")
        msg = o.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            content = [b for b in content if isinstance(b, dict)]
        else:
            content = []
        uuid = o.get("uuid")
        if typ == "user":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            results = [b for b in content if b.get("type") == "tool_result"]
            imgs = []
            for b in content:
                if b.get("type") == "image":
                    src = b.get("source") or {}
                    if src.get("type") == "base64":
                        mt = src.get("media_type", "image/png")
                        imgs.append({"url": f"data:{mt};base64,{src.get('data', '')}",
                                     "mime": mt, "name": ""})
            if text or imgs:
                out.append(ev.UserMessage(turn_id=uuid, text=text, src_uuid=uuid,
                                          images=imgs or None))
            for r in results:
                out.append(ev.ToolResult(r["tool_use_id"], not r.get("is_error", False),
                                         {"content": r.get("content")}, uuid))
        elif typ == "assistant":
            model = msg.get("model")
            if model and meta_event is None:
                meta_event = ev.SessionMeta(model=model, session_id=o.get("sessionId") or "")
            out.append(ev.TurnStart(turn_id=uuid, src_uuid=uuid))
            for b in content:
                t = b.get("type")
                if t == "text":
                    out.append(ev.TextDelta(uuid, b.get("text", ""), uuid))
                elif t == "thinking":
                    out.append(ev.ThinkingDelta(uuid, b.get("thinking", ""), uuid))
                elif t == "tool_use":
                    if b.get("name") == "AskUserQuestion":
                        for qi, q in enumerate((b.get("input") or {}).get("questions") or []):
                            if not isinstance(q, dict):
                                continue
                            opts = [o2 if isinstance(o2, dict) else {"label": str(o2)}
                                    for o2 in (q.get("options") or [])]
                            out.append(ev.Question(
                                question_id=f"{b.get('id') or uuid}-{qi}",
                                turn_id=uuid,
                                header=q.get("header", ""),
                                question=q.get("question", ""),
                                options=opts,
                                multi_select=bool(q.get("multiSelect", False)),
                            ))
                    else:
                        out.append(ev.ToolCall(uuid, b.get("id", ""), b.get("name", ""),
                                               ev.tool_kind(b.get("name", "")), b.get("input", {}), uuid))
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            out.append(ev.TurnEnd(uuid, usage, None, uuid))
    return out, meta_event


def _window(path: str, before: int | None, limit: int) -> ReplayWindow:
    out, meta_event = _build_events(path)
    starts = [i for i, e in enumerate(out) if isinstance(e, ev.TurnStart)]
    total = len(starts)
    if total == 0:
        return ReplayWindow(events=[], earliest_turn=0, total_turns=0)
    end = total if before is None else max(0, min(before, total))
    start = max(0, end - limit)
    slice_lo = starts[start]
    slice_hi = starts[end] if end < total else len(out)
    window = out[slice_lo:slice_hi]
    if meta_event is not None:
        window = [meta_event] + window
    return ReplayWindow(events=window, earliest_turn=start, total_turns=total)


def _parse(path: str, max_turns: int) -> list:
    """Back-compat: last `max_turns` turns as a flat event list."""
    return _window(path, before=None, limit=max_turns).events


async def replay_window(jsonl_path: str, *, before: int | None = None, limit: int = 20) -> ReplayWindow:
    return await asyncio.to_thread(_window, jsonl_path, before, limit)


async def replay_events(jsonl_path: str, *, max_turns: int = 20) -> list:
    return await asyncio.to_thread(_parse, jsonl_path, max_turns)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_replay_window.py server/tests/ -k replay -v`
Expected: PASS (new tests + any existing replay tests still green).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/replay.py server/tests/test_replay_window.py
git commit -m "feat(replay): turn-ordinal windowed replay (replay_window)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Backend — WS attach reports history extent

**Files:**
- Modify: `server/bridge/ws.py` (`_replay_and_complete`, ~169-196)
- Test: `server/tests/test_ws.py` (add a test; mirror existing replay tests' setup)

**Interfaces:**
- Produces: the primary `replayComplete` frame now carries `{"type":"replayComplete","earliestTurn":int,"totalTurns":int}`. Error-path `replayComplete` frames stay bare (no history known). The replay batch is built from `replay_window(path)` instead of `replay_events(path)`.
- Consumes: `from server.sessions.replay import replay_window` (replace/augment the `replay_events` import).

- [ ] **Step 1: Write the failing test**

In `server/tests/test_ws.py`, add (adapt the existing fixture that drives a fake transcript through attach — find the test that asserts on a `replayComplete` frame and copy its harness):

```python
@pytest.mark.asyncio
async def test_replay_complete_reports_turn_extent(ws_harness_with_transcript):
    # ws_harness_with_transcript: a 25-turn transcript wired to attach (reuse the
    # existing transcript-replay fixture; if none, build one like test_replay_window).
    frames = await ws_harness_with_transcript.attach_and_collect()
    done = [f for f in frames if f.get("type") == "replayComplete"]
    assert done, "replayComplete must be sent"
    assert done[-1]["totalTurns"] == 25
    assert done[-1]["earliestTurn"] == 5   # last 20 of 25
```

> If `test_ws.py` has no transcript-replay harness, add a minimal one: write a JSONL via the `_write_transcript` helper pattern from Task 1, monkeypatch `orchestrator.transcript_path` to return it, and collect frames sent through the WS `_send` stub. Keep `CCSWITCH_TAILNET_ALLOWLIST` pinned per the project test note.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_ws.py -k turn_extent -v`
Expected: FAIL (KeyError `totalTurns` / bare replayComplete).

- [ ] **Step 3: Implement**

In `server/bridge/ws.py`, update `_replay_and_complete` to use `replay_window` and enrich the completion frame:

```python
async def _replay_and_complete(session_id: str) -> list:
    events: list = []
    earliest_turn = 0
    total_turns = 0
    try:
        path = await orchestrator.transcript_path(session_id)
        if path:
            window = await replay_window(path)
            events = window.events
            earliest_turn = window.earliest_turn
            total_turns = window.total_turns
        if events:
            redacted_replay = [{"type": "replayReset"}]
            redacted_replay += [redact_event(e.to_wire()) for e in events]
            await _send({"type": "batch", "events": redacted_replay})
    except Exception:
        log.warning("ws.attach: transcript replay failed for %s", session_id, exc_info=True)
    await _send({"type": "replayComplete", "earliestTurn": earliest_turn, "totalTurns": total_turns})
    return events
```

Add the import near the existing replay import:

```python
from server.sessions.replay import replay_window
```

Leave the error-path `replayComplete` sends (lines ~219/240/246) as bare frames — the client treats missing fields as "no history known" (Task 5 defaults them to 0).

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_ws.py -v`
Expected: PASS (new test + existing WS tests green).

- [ ] **Step 5: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws.py
git commit -m "feat(ws): replayComplete reports earliestTurn/totalTurns

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Backend — transcript endpoint paging

**Files:**
- Modify: `server/api/sessions.py` (`get_session_transcript`, ~488-517)
- Modify: `server/sessions/orchestrator.py` (`transcript_events`, ~1593) — add a windowed sibling
- Test: `server/tests/test_api_sessions.py` (add cases) or the existing transcript-endpoint test file

**Interfaces:**
- Produces:
  - `orchestrator.transcript_window(session_id, *, before, limit) -> ReplayWindow | None` (None when no transcript path).
  - `GET /api/sessions/{session_id}/transcript` accepts optional `before: int | None`, `limit: int = 20`. With **no** `before`, response is `{"events":[...]}` (unchanged) — backward compatible. With `before` present, response is `{"events":[...], "earliestTurn": int, "totalTurns": int}`.
- Consumes: `ReplayWindow` (Task 1), `replay_window` (Task 1), `transcript_path` (existing).

- [ ] **Step 1: Write the failing tests**

In the transcript-endpoint test module, add (pin `CCSWITCH_TAILNET_ALLOWLIST` to the test identity per the existing tests in that file):

```python
@pytest.mark.asyncio
async def test_transcript_paging_returns_window_and_meta(client, seeded_session_25_turns):
    sid = seeded_session_25_turns
    r = await client.get(f"/api/sessions/{sid}/transcript?before=10&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["totalTurns"] == 25
    assert body["earliestTurn"] == 5          # turns 5..9
    starts = [e for e in body["events"] if e["type"] == "turnStart"]
    assert len(starts) == 5


@pytest.mark.asyncio
async def test_transcript_no_params_is_backward_compatible(client, seeded_session_25_turns):
    sid = seeded_session_25_turns
    r = await client.get(f"/api/sessions/{sid}/transcript")
    assert r.status_code == 200
    assert set(r.json().keys()) == {"events"}   # no extra keys when unpaged


@pytest.mark.asyncio
async def test_transcript_unknown_session_404(client):
    r = await client.get("/api/sessions/does-not-exist/transcript?before=5")
    assert r.status_code == 404
```

> `seeded_session_25_turns`: reuse/extend the existing transcript test fixture to write a 25-turn JSONL discoverable by `transcript_path` (the existing transcript-endpoint test already wires one — parameterize its turn count, or add a sibling fixture).

- [ ] **Step 2: Run tests to verify they fail**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/ -k "transcript_paging or backward_compatible or unknown_session_404" -v`
Expected: FAIL (unexpected keys / 200 with wrong shape).

- [ ] **Step 3: Implement orchestrator + endpoint**

In `server/sessions/orchestrator.py` add beside `transcript_events`:

```python
async def transcript_window(self, session_id, *, before: int | None = None, limit: int = 20):
    """Windowed transcript replay. Returns a ReplayWindow, or None if no transcript."""
    from server.sessions.replay import replay_window
    path = self.transcript_path(session_id)
    if not path:
        return None
    try:
        return await replay_window(path, before=before, limit=limit)
    except Exception:
        log.warning("session.transcript_window id=%s replay failed", session_id, exc_info=True)
        return None
```

In `server/api/sessions.py`, update the endpoint signature and body:

```python
@router.get("/{session_id}/transcript")
async def get_session_transcript(
    session_id: str,
    request: Request,
    before: int | None = None,
    limit: int = 20,
    _identity: str = Depends(current_identity),
) -> dict:
    """Read-only transcript events. With `before`, returns a turn-ordinal window
    plus earliestTurn/totalTurns for lazy scrollback paging."""
    from server.db import db_session as _db_session
    from server.db.models import Session as _DbSession
    from server.audit import redact_event
    from sqlalchemy import select as sa_select

    orch = _get_orchestrator(request)

    record = await orch.get_session(session_id)
    if record is None:
        async with _db_session() as _db:
            row = (await _db.execute(
                sa_select(_DbSession).where(_DbSession.id == session_id)
            )).scalars().first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
            )

    if before is None:
        events = await orch.transcript_events(session_id)
        return {"events": [redact_event(e.to_wire()) for e in events]}

    window = await orch.transcript_window(session_id, before=before, limit=limit)
    if window is None:
        return {"events": [], "earliestTurn": 0, "totalTurns": 0}
    return {
        "events": [redact_event(e.to_wire()) for e in window.events],
        "earliestTurn": window.earliest_turn,
        "totalTurns": window.total_turns,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/ -k "transcript" -v`
Expected: PASS (new paging tests + existing transcript tests green).

- [ ] **Step 5: Commit**

```bash
git add server/api/sessions.py server/sessions/orchestrator.py server/tests/
git commit -m "feat(api): paged transcript window (?before&limit) with extent meta

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Backend — transcript export endpoints

**Files:**
- Create: `server/sessions/export.py` (Markdown formatter)
- Modify: `server/api/sessions.py` (two GET endpoints)
- Test: `server/tests/test_export.py` (create)

**Interfaces:**
- Produces:
  - `transcript_markdown(events: list, *, title: str) -> str` in `server/sessions/export.py`, taking redacted wire-event dicts.
  - `GET /api/sessions/{session_id}/export.md` → `text/markdown; charset=utf-8`, `Content-Disposition: attachment; filename="<safe>.md"`.
  - `GET /api/sessions/{session_id}/export.jsonl` → `application/x-ndjson`, attachment, **raw unredacted** file bytes.
- Consumes: `transcript_path` (existing), `transcript_events` (existing), `redact_event`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_export.py`:

```python
from __future__ import annotations
from server.sessions.export import transcript_markdown


def test_markdown_renders_roles_and_tools():
    events = [
        {"type": "sessionMeta", "model": "claude-x", "sessionId": "s1"},
        {"type": "user", "turnId": "u0", "text": "hello", "images": []},
        {"type": "turnStart", "turnId": "a0"},
        {"type": "textDelta", "turnId": "a0", "text": "hi there"},
        {"type": "thinkingDelta", "turnId": "a0", "text": "ponder"},
        {"type": "toolCall", "turnId": "a0", "toolId": "t1", "name": "Bash",
         "toolKind": "bash", "input": {"command": "ls"}},
        {"type": "toolResult", "toolId": "t1", "ok": True, "payload": {"content": "file.txt"}},
        {"type": "turnEnd", "turnId": "a0", "usage": None},
    ]
    md = transcript_markdown(events, title="My Session")
    assert "# My Session" in md
    assert "## User" in md and "hello" in md
    assert "## Assistant" in md and "hi there" in md
    assert "Bash" in md and "ls" in md          # tool call rendered
    assert "file.txt" in md                      # tool result rendered
    assert "claude-x" in md                      # model in header


def test_markdown_empty_is_safe():
    assert isinstance(transcript_markdown([], title="Empty"), str)
```

And API-level tests (in the export test file or the api test module — reuse the 25-turn transcript fixture):

```python
@pytest.mark.asyncio
async def test_export_md_download(client, seeded_session_25_turns):
    r = await client.get(f"/api/sessions/{seeded_session_25_turns}/export.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.md"')


@pytest.mark.asyncio
async def test_export_jsonl_raw_download(client, seeded_session_25_turns):
    r = await client.get(f"/api/sessions/{seeded_session_25_turns}/export.jsonl")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in r.headers["content-disposition"]
    # raw passthrough: first non-empty line parses as JSON
    import json
    first = next(l for l in r.text.splitlines() if l.strip())
    json.loads(first)


@pytest.mark.asyncio
async def test_export_missing_transcript_404(client, session_without_transcript):
    r = await client.get(f"/api/sessions/{session_without_transcript}/export.md")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_export.py -v`
Expected: FAIL (`ModuleNotFoundError: server.sessions.export`).

- [ ] **Step 3: Implement formatter + endpoints**

Create `server/sessions/export.py`:

```python
from __future__ import annotations
import json
from typing import Any


def _fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


def transcript_markdown(events: list[dict[str, Any]], *, title: str) -> str:
    model = next((e.get("model") for e in events if e.get("type") == "sessionMeta"), None)
    lines: list[str] = [f"# {title}"]
    if model:
        lines.append(f"_Model: {model}_")
    lines.append("")
    for e in events:
        t = e.get("type")
        if t == "user":
            lines += ["## User", "", e.get("text", ""), ""]
            for img in e.get("images") or []:
                lines += [f"_[image: {img.get('name') or img.get('mime', 'image')}]_", ""]
        elif t == "turnStart":
            lines += ["## Assistant", ""]
        elif t == "textDelta":
            lines += [e.get("text", ""), ""]
        elif t == "thinkingDelta":
            lines += ["> _thinking_", "> " + e.get("text", "").replace("\n", "\n> "), ""]
        elif t == "toolCall":
            inp = json.dumps(e.get("input", {}), indent=2, ensure_ascii=False)
            lines += [f"**Tool: {e.get('name', '')}**", _fence(inp, "json"), ""]
        elif t == "toolResult":
            payload = e.get("payload", {})
            content = payload.get("content") if isinstance(payload, dict) else payload
            body = content if isinstance(content, str) else json.dumps(content, indent=2, ensure_ascii=False)
            status = "ok" if e.get("ok") else "error"
            lines += [f"**Result ({status})**", _fence(str(body)), ""]
        elif t == "question":
            lines += [f"**Question: {e.get('question', '')}**", ""]
    return "\n".join(lines) + "\n"


def safe_filename(stem: str) -> str:
    keep = [c if c.isalnum() or c in "-_." else "-" for c in stem]
    return "".join(keep).strip("-") or "session"
```

In `server/api/sessions.py` add (imports: `from fastapi.responses import Response, StreamingResponse`; reuse `redact_event`):

```python
@router.get("/{session_id}/export.md")
async def export_session_markdown(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
):
    from server.audit import redact_event
    from server.sessions.export import transcript_markdown, safe_filename
    orch = _get_orchestrator(request)
    path = orch.transcript_path(session_id)
    if not path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "No transcript"})
    record = await orch.get_session(session_id)
    title = (getattr(record, "name", None) or getattr(record, "title", None) or session_id) if record else session_id
    events = await orch.transcript_events(session_id)
    md = transcript_markdown([redact_event(e.to_wire()) for e in events], title=str(title))
    fname = safe_filename(f"{title}-{session_id}") + ".md"
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/{session_id}/export.jsonl")
async def export_session_jsonl(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
):
    from server.sessions.export import safe_filename
    orch = _get_orchestrator(request)
    path = orch.transcript_path(session_id)
    if not path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "No transcript"})

    def _iter():
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    fname = safe_filename(f"{session_id}") + ".jsonl"
    return StreamingResponse(_iter(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})
```

> Note: `transcript_path` is sync (no `await`). `session_without_transcript` fixture: a session whose `transcript_path` returns None (e.g. no `claude_session_id`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_export.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/export.py server/api/sessions.py server/tests/test_export.py
git commit -m "feat(api): transcript export endpoints (markdown + raw jsonl)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Frontend — reducer history state, prepend, metadata

**Files:**
- Modify: `web/src/session/reducer.ts`
- Modify: `web/src/session/events.ts` (extend `EvReplayComplete`)
- Test: `web/src/session/reducer.history.test.ts` (create)

**Interfaces:**
- Produces:
  - `TranscriptState` gains `earliestTurn: number`, `totalTurns: number`, `loadingOlder: boolean` (init 0/0/false).
  - `LocalAction` gains `{ type: '_prependHistory'; events: SessionEvent[]; earliestTurn: number }` and `{ type: '_setLoadingOlder'; value: boolean }`.
  - Exported helper `buildTurnsFromEvents(events: SessionEvent[]): Turn[]`.
  - `replayComplete` records `earliestTurn`/`totalTurns` from the event; `replayReset` resets them to 0 and `loadingOlder` to false.
- Consumes: `EvReplayComplete` now `{ type: 'replayComplete'; earliestTurn?: number; totalTurns?: number }`.

- [ ] **Step 1: Write the failing tests**

Create `web/src/session/reducer.history.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript, buildTurnsFromEvents } from './reducer'
import type { SessionEvent } from './events'

const olderTurn: SessionEvent[] = [
  { type: 'user', turnId: 'u-old', text: 'old question' },
  { type: 'turnStart', turnId: 'a-old' },
  { type: 'textDelta', turnId: 'a-old', text: 'old answer' },
  { type: 'turnEnd', turnId: 'a-old' },
]

describe('history paging reducer', () => {
  it('records extent on replayComplete', () => {
    const s = transcriptReducer(initialTranscript,
      { type: 'replayComplete', earliestTurn: 5, totalTurns: 25 } as SessionEvent)
    expect(s.earliestTurn).toBe(5)
    expect(s.totalTurns).toBe(25)
    expect(s.replaying).toBe(false)
  })

  it('prepends older turns ahead of existing turns and lowers earliestTurn', () => {
    let s = { ...initialTranscript, earliestTurn: 5,
      turns: [{ id: 'a-cur', role: 'assistant' as const, blocks: [] }] }
    s = transcriptReducer(s, { type: '_prependHistory', events: olderTurn, earliestTurn: 4 } as never)
    expect(s.turns[0].id).toBe('u-old')   // older turn now first
    expect(s.turns[s.turns.length - 1].id).toBe('a-cur')
    expect(s.earliestTurn).toBe(4)
    expect(s.loadingOlder).toBe(false)
  })

  it('replayReset clears extent', () => {
    let s = { ...initialTranscript, earliestTurn: 5, totalTurns: 25 }
    s = transcriptReducer(s, { type: 'replayReset' } as SessionEvent)
    expect(s.earliestTurn).toBe(0)
    expect(s.totalTurns).toBe(0)
  })

  it('buildTurnsFromEvents reuses the reducer to produce turns', () => {
    const turns = buildTurnsFromEvents(olderTurn)
    expect(turns.map((t) => t.id)).toEqual(['u-old', 'a-old'])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/reducer.history.test.ts`
Expected: FAIL (`buildTurnsFromEvents` undefined; fields undefined).

- [ ] **Step 3: Implement**

In `web/src/session/events.ts`, extend the type:

```ts
export interface EvReplayComplete { type: 'replayComplete'; earliestTurn?: number; totalTurns?: number }
```

In `web/src/session/reducer.ts`:

Add fields to `TranscriptState` (after `replaying`):

```ts
  earliestTurn: number          // lowest turn ordinal currently loaded (0 = at session start)
  totalTurns: number            // total turns in the on-disk transcript
  loadingOlder: boolean         // a prepend fetch is in flight
```

Add to `initialTranscript`: `earliestTurn: 0, totalTurns: 0, loadingOlder: false,`.

Extend `LocalAction`:

```ts
  | { type: '_prependHistory'; events: SessionEvent[]; earliestTurn: number }
  | { type: '_setLoadingOlder'; value: boolean }
```

Update `replayReset` case (line ~517) to also reset extent:

```ts
      return { ...state, turns: [], openBlockId: null, subAgents: {}, replaying: true,
               earliestTurn: 0, totalTurns: 0, loadingOlder: false }
```

Update `replayComplete` case (line ~521):

```ts
    case 'replayComplete': {
      return { ...state, replaying: false,
               earliestTurn: ev.earliestTurn ?? state.earliestTurn,
               totalTurns: ev.totalTurns ?? state.totalTurns }
    }
```

Add the two local-action cases (anywhere in the switch, e.g. after `replayComplete`):

```ts
    case '_setLoadingOlder': {
      return { ...state, loadingOlder: ev.value }
    }
    case '_prependHistory': {
      const olderTurns = buildTurnsFromEvents(ev.events)
      // Drop any older turn whose id already exists (defensive; windows are disjoint server-side).
      const existing = new Set(state.turns.map((t) => t.id))
      const fresh = olderTurns.filter((t) => !existing.has(t.id))
      return { ...state, turns: [...fresh, ...state.turns],
               earliestTurn: ev.earliestTurn, loadingOlder: false }
    }
```

Add the exported helper at the bottom of the file:

```ts
/**
 * Fold a window of replayed events into a fresh transcript and return just its
 * turns. Reuses transcriptReducer so history paging never grows a second
 * turn-builder (single source of truth for event→Turn translation).
 */
export function buildTurnsFromEvents(events: SessionEvent[]): Turn[] {
  const seeded: TranscriptState = { ...initialTranscript, replaying: true }
  const final = events.reduce(transcriptReducer, seeded)
  return final.turns
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/reducer.history.test.ts && ./node_modules/.bin/tsc --noEmit`
Expected: PASS + no type errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/reducer.ts web/src/session/events.ts web/src/session/reducer.history.test.ts
git commit -m "feat(web): reducer history paging (prepend + extent metadata)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Frontend — API client (paging + export)

**Files:**
- Modify: `web/src/api/client.ts` (or add `web/src/api/transcript.ts` — match where session calls live)
- Test: `web/src/api/__tests__/transcript.test.ts` (create)

**Interfaces:**
- Produces:
  - `fetchOlderTurns(sessionId: string, before: number, limit?: number): Promise<{ events: SessionEvent[]; earliestTurn: number; totalTurns: number }>`.
  - `exportTranscriptUrl(sessionId: string, format: 'md' | 'jsonl'): string` → `/api/sessions/${id}/export.${format}`.
  - `downloadTranscript(sessionId, format)` → triggers a browser download via a temporary anchor.
- Consumes: `api.get` (existing); `SessionEvent` type.

- [ ] **Step 1: Write the failing test**

Create `web/src/api/__tests__/transcript.test.ts`:

```ts
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fetchOlderTurns, exportTranscriptUrl } from '../transcript'

afterEach(() => vi.restoreAllMocks())

describe('transcript api', () => {
  it('fetchOlderTurns requests the paged endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ events: [], earliestTurn: 0, totalTurns: 9 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const res = await fetchOlderTurns('sid', 10, 5)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/sessions/sid/transcript?before=10&limit=5', expect.anything())
    expect(res.totalTurns).toBe(9)
  })

  it('exportTranscriptUrl builds the right path', () => {
    expect(exportTranscriptUrl('sid', 'md')).toBe('/api/sessions/sid/export.md')
    expect(exportTranscriptUrl('sid', 'jsonl')).toBe('/api/sessions/sid/export.jsonl')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/api/__tests__/transcript.test.ts`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

Create `web/src/api/transcript.ts`:

```ts
import { api } from './client'
import type { SessionEvent } from '@/session/events'

export interface OlderTurns {
  events: SessionEvent[]
  earliestTurn: number
  totalTurns: number
}

export function fetchOlderTurns(sessionId: string, before: number, limit = 20): Promise<OlderTurns> {
  return api.get<OlderTurns>(`/sessions/${sessionId}/transcript?before=${before}&limit=${limit}`)
}

export function exportTranscriptUrl(sessionId: string, format: 'md' | 'jsonl'): string {
  return `/api/sessions/${sessionId}/export.${format}`
}

/** Trigger a browser download of the transcript export. */
export function downloadTranscript(sessionId: string, format: 'md' | 'jsonl'): void {
  const a = document.createElement('a')
  a.href = exportTranscriptUrl(sessionId, format)
  a.download = ''
  document.body.appendChild(a)
  a.click()
  a.remove()
}
```

> `api.get` builds `/api${path}`, so pass `/sessions/...` (the test asserts the full `/api/...` URL the underlying fetch receives).

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `./node_modules/.bin/vitest run src/api/__tests__/transcript.test.ts && ./node_modules/.bin/tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/transcript.ts web/src/api/__tests__/transcript.test.ts
git commit -m "feat(web): transcript paging + export api client

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Frontend — runtime wiring (`onLoadOlder`)

**Files:**
- Modify: `web/src/session/useSessionRuntime.ts`
- Test: `web/src/session/useSessionRuntime.test.ts` (add a case; reuse its existing harness)

**Interfaces:**
- Produces: `SessionRuntime` gains `onLoadOlder: () => void`. It guards re-entrancy via `state.loadingOlder`/`state.earliestTurn`, dispatches `_setLoadingOlder true`, calls `fetchOlderTurns(sessionId, state.earliestTurn)`, then dispatches `_prependHistory` with the response (or `_setLoadingOlder false` on error/empty).
- Consumes: `fetchOlderTurns` (Task 6); reducer actions (Task 5).

- [ ] **Step 1: Write the failing test**

In `web/src/session/useSessionRuntime.test.ts`, add (mock `@/api/transcript`):

```ts
it('onLoadOlder fetches before earliestTurn and prepends', async () => {
  const { fetchOlderTurns } = await import('@/api/transcript')
  vi.mocked(fetchOlderTurns).mockResolvedValue({
    events: [{ type: 'user', turnId: 'u-old', text: 'old' }],
    earliestTurn: 0, totalTurns: 21,
  })
  const { result } = renderHook(() => useSessionRuntime('sid'))
  // Simulate a prior replay extent.
  act(() => { result.current.state /* extent set via replayComplete in harness */ })
  await act(async () => { result.current.onLoadOlder() })
  expect(fetchOlderTurns).toHaveBeenCalledWith('sid', expect.any(Number))
})
```

> Adapt to the file's existing harness (it already mocks `@/ws/client`). Add `vi.mock('@/api/transcript')` at top. If driving `replayComplete` through the harness is awkward, assert the guard instead: `onLoadOlder` is a no-op when `earliestTurn === 0` (call it on a fresh runtime and assert `fetchOlderTurns` was NOT called).

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/useSessionRuntime.test.ts`
Expected: FAIL (`onLoadOlder` undefined).

- [ ] **Step 3: Implement**

In `useSessionRuntime.ts`, add near the other `useCallback` handlers (and import at top: `import { fetchOlderTurns } from '@/api/transcript'`):

```ts
  const loadingOlderRef = useRef(false)
  const onLoadOlder = useCallback(() => {
    if (loadingOlderRef.current) return
    if (state.earliestTurn <= 0) return     // already at session start
    loadingOlderRef.current = true
    dispatch({ type: '_setLoadingOlder', value: true })
    fetchOlderTurns(sessionId, state.earliestTurn)
      .then((res) => {
        dispatch({ type: '_prependHistory', events: res.events, earliestTurn: res.earliestTurn })
      })
      .catch(() => {
        dispatch({ type: '_setLoadingOlder', value: false })
      })
      .finally(() => { loadingOlderRef.current = false })
  }, [sessionId, state.earliestTurn])
```

Add `onLoadOlder` to the returned object and to the `SessionRuntime` interface:

```ts
  /** Fetch and prepend the previous window of older turns (lazy scrollback). */
  onLoadOlder: () => void
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/useSessionRuntime.test.ts && ./node_modules/.bin/tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useSessionRuntime.ts web/src/session/useSessionRuntime.test.ts
git commit -m "feat(web): onLoadOlder runtime wiring for lazy scrollback

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Frontend — TranscriptView scroll-to-top paging + position preservation

**Files:**
- Modify: `web/src/components/session/TranscriptView.tsx`
- Modify: `web/src/components/session/SessionBody.tsx` (pass new props)
- Test: `web/src/components/session/TranscriptView.test.tsx` (create)

**Interfaces:**
- Produces: `TranscriptView` accepts new optional props `earliestTurn?: number`, `loadingOlder?: boolean`, `onLoadOlder?: () => void`. Calls `onLoadOlder()` when scrolled near the top and `earliestTurn > 0` and not `loadingOlder`. Preserves scroll position across a prepend. Renders a top spinner while `loadingOlder` and a "Beginning of session" marker when `earliestTurn === 0` and there are turns.
- Consumes: `rt.state.earliestTurn`, `rt.state.loadingOlder`, `rt.onLoadOlder` (Tasks 5/7).

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/TranscriptView.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { TranscriptView } from './TranscriptView'
import type { Turn } from '@/session/reducer'

const turns: Turn[] = [{ id: 't1', role: 'assistant', blocks: [{ kind: 'text', id: 't1:x', text: 'hi' }] }]
const base = {
  turns, permissionMode: 'interactive', toolDetailMode: 'auto' as const,
  thinkingCollapsed: true, onDecision: () => {}, onAnswer: () => {},
}

describe('TranscriptView scrollback', () => {
  it('calls onLoadOlder when scrolled to top and more history exists', () => {
    const onLoadOlder = vi.fn()
    const { container } = render(
      <TranscriptView {...base} earliestTurn={5} loadingOlder={false} onLoadOlder={onLoadOlder} />)
    const scroller = container.firstChild as HTMLElement
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    fireEvent.scroll(scroller)
    expect(onLoadOlder).toHaveBeenCalled()
  })

  it('does not call onLoadOlder at session start', () => {
    const onLoadOlder = vi.fn()
    const { container } = render(
      <TranscriptView {...base} earliestTurn={0} loadingOlder={false} onLoadOlder={onLoadOlder} />)
    const scroller = container.firstChild as HTMLElement
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    fireEvent.scroll(scroller)
    expect(onLoadOlder).not.toHaveBeenCalled()
  })

  it('shows the beginning-of-session marker at the start', () => {
    const { getByText } = render(<TranscriptView {...base} earliestTurn={0} />)
    expect(getByText(/beginning of session/i)).toBeTruthy()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/session/TranscriptView.test.tsx`
Expected: FAIL (props not handled; marker absent).

- [ ] **Step 3: Implement**

In `TranscriptView.tsx`, extend `Props` with the three optional fields, then update the component:

```tsx
  earliestTurn?: number
  loadingOlder?: boolean
  onLoadOlder?: () => void
```

Add to the component body:

```tsx
  const TOP_THRESHOLD = 64
  const prevScrollHeightRef = useRef<number | null>(null)
  const prevTurnCountRef = useRef(turns.length)

  // Before a prepend lands, capture scroll height so we can restore the anchor.
  if (turns.length > prevTurnCountRef.current && (containerRef.current)) {
    // a prepend (or append) changed the count — handled in the layout effect below
  }

  // Preserve scroll position when older turns are prepended (turns grew at the top).
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (prevScrollHeightRef.current != null) {
      el.scrollTop += el.scrollHeight - prevScrollHeightRef.current
      prevScrollHeightRef.current = null
    }
    prevTurnCountRef.current = turns.length
  }, [turns])
```

Update `handleScroll` to also trigger paging and snapshot height for the prepend:

```tsx
  function handleScroll() {
    const el = containerRef.current
    if (!el) return
    const threshold = 80
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
    if (el.scrollTop <= TOP_THRESHOLD && (earliestTurn ?? 0) > 0 && !loadingOlder && onLoadOlder) {
      prevScrollHeightRef.current = el.scrollHeight   // anchor before the prepend
      onLoadOlder()
    }
  }
```

Guard the existing auto-scroll-to-bottom effect so a prepend never yanks the view down — it already checks `isNearBottomRef.current`, which is false when reading old history, so no change needed; leave it.

Add the top affordances inside the scroll container, before the `top.map(...)` rotations:

```tsx
      {loadingOlder && (
        <div className="py-2 text-center text-xs text-gray-400">Loading earlier history…</div>
      )}
      {turns.length > 0 && (earliestTurn ?? 0) === 0 && (
        <div className="py-2 text-center text-xs text-gray-400">— Beginning of session —</div>
      )}
```

In `SessionBody.tsx`, pass the new props on the `<TranscriptView>` at line 158:

```tsx
                earliestTurn={rt.state.earliestTurn}
                loadingOlder={rt.state.loadingOlder}
                onLoadOlder={rt.onLoadOlder}
```

> Leave `ReviewView.tsx` and `SubSessionView.tsx` callers unchanged — the new props are optional, so read-only views simply don't page.

- [ ] **Step 4: Run tests to verify they pass**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/session/TranscriptView.test.tsx && ./node_modules/.bin/tsc --noEmit && ./node_modules/.bin/eslint src/components/session/TranscriptView.tsx src/components/session/SessionBody.tsx`
Expected: PASS, no type/lint errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/TranscriptView.tsx web/src/components/session/SessionBody.tsx web/src/components/session/TranscriptView.test.tsx
git commit -m "feat(web): scroll-to-top lazy paging with position preservation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Frontend — Export control in the session header

**Files:**
- Modify: the session header component (find with `grep -rn "title" web/src/components/session/SessionHeader*.tsx` or the header rendered above `SessionBody`)
- Test: extend that header's test or add `web/src/components/session/ExportMenu.test.tsx`

**Interfaces:**
- Produces: an Export control (button/menu) with two items — "Markdown" and "Raw JSONL" — each calling `downloadTranscript(sessionId, 'md' | 'jsonl')` (Task 6).
- Consumes: `downloadTranscript` (Task 6); the `sessionId` already available in the header.

- [ ] **Step 1: Locate the header and write the failing test**

Run: `grep -rln "account\|quota\|status strip\|Header" web/src/components/session/*.tsx` to find the header that renders the title/account strip. Create `web/src/components/session/ExportMenu.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { ExportMenu } from './ExportMenu'

vi.mock('@/api/transcript', () => ({ downloadTranscript: vi.fn() }))

describe('ExportMenu', () => {
  it('downloads markdown when the Markdown item is clicked', async () => {
    const { downloadTranscript } = await import('@/api/transcript')
    const { getByRole, getByText } = render(<ExportMenu sessionId="sid" />)
    fireEvent.click(getByRole('button', { name: /export/i }))
    fireEvent.click(getByText(/markdown/i))
    expect(downloadTranscript).toHaveBeenCalledWith('sid', 'md')
  })

  it('downloads raw jsonl when that item is clicked', async () => {
    const { downloadTranscript } = await import('@/api/transcript')
    const { getByRole, getByText } = render(<ExportMenu sessionId="sid" />)
    fireEvent.click(getByRole('button', { name: /export/i }))
    fireEvent.click(getByText(/jsonl/i))
    expect(downloadTranscript).toHaveBeenCalledWith('sid', 'jsonl')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/session/ExportMenu.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

Create `web/src/components/session/ExportMenu.tsx` (match existing menu/dropdown patterns in the codebase; a minimal accessible version):

```tsx
import { useState } from 'react'
import { downloadTranscript } from '@/api/transcript'

export function ExportMenu({ sessionId }: { sessionId: string }) {
  const [open, setOpen] = useState(false)
  function pick(format: 'md' | 'jsonl') {
    downloadTranscript(sessionId, format)
    setOpen(false)
  }
  return (
    <div className="relative">
      <button
        type="button"
        aria-label="Export transcript"
        onClick={() => setOpen((o) => !o)}
        className="px-2 py-1 text-xs rounded hover:bg-gray-100 dark:hover:bg-gray-800"
      >
        Export
      </button>
      {open && (
        <div role="menu" className="absolute right-0 mt-1 z-10 rounded border bg-white dark:bg-gray-900 shadow text-sm">
          <button role="menuitem" type="button" onClick={() => pick('md')}
            className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800">Markdown</button>
          <button role="menuitem" type="button" onClick={() => pick('jsonl')}
            className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800">Raw JSONL</button>
        </div>
      )}
    </div>
  )
}
```

Render `<ExportMenu sessionId={sessionId} />` in the located session header, next to the existing title/status controls.

- [ ] **Step 4: Run tests to verify they pass**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/session/ExportMenu.test.tsx && ./node_modules/.bin/tsc --noEmit && ./node_modules/.bin/eslint src/components/session/ExportMenu.tsx`
Expected: PASS, no type/lint errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/ExportMenu.tsx web/src/components/session/ExportMenu.test.tsx <header-file>
git commit -m "feat(web): transcript export menu in session header

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Server suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/ -q`
Expected: all pass.

- [ ] **Step 2: Web suite**

Run (in `web/`): `./node_modules/.bin/vitest run && ./node_modules/.bin/tsc --noEmit && ./node_modules/.bin/eslint src`
Expected: all pass, no type/lint errors.

- [ ] **Step 3: Manual smoke (optional, via `/run` or live host)**

Open a long session, scroll to the top → older turns load and the viewport stays anchored; reach the start → "Beginning of session" marker. Click Export → Markdown and Raw JSONL both download.

- [ ] **Step 4: Open PR**

Push the branch and open a non-draft PR; enable auto-merge (`gh pr merge <#> --squash --auto`). PR body ends with the Claude Code trailer.

---

## Self-Review

**Spec coverage:** windowed replay (T1) · WS extent (T2) · paged endpoint (T3) · export md+jsonl (T4) · reducer prepend/extent (T5) · api client (T6) · runtime wiring (T7) · scroll paging + position preservation + markers (T8) · export UI (T9) · verification (T10). All spec sections map to a task. Known limitations (re-parse cost, sub-agent threads, rotation re-anchoring) are inherited from the spec and need no task.

**Placeholder scan:** every code step shows full code; the only `<header-file>` placeholder in T9 is an explicit locate-then-edit step with the grep to find it (the file genuinely varies and is discovered at execution).

**Type consistency:** `ReplayWindow{events, earliest_turn, total_turns}` (T1) flows to WS (T2), endpoint (T3); wire `earliestTurn`/`totalTurns` (camelCase) consistent across T2/T3/T5/T6. `fetchOlderTurns(sessionId, before, limit?)` consistent T6/T7. `_prependHistory{events, earliestTurn}` / `_setLoadingOlder{value}` consistent T5/T7. `onLoadOlder`/`earliestTurn`/`loadingOlder` consistent T7/T8. `downloadTranscript(sessionId, 'md'|'jsonl')` consistent T6/T9.
