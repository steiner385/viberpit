# Per-block Transcript Timestamps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a small, always-visible, server-stamped timestamp on each block of the session transcript.

**Architecture:** Backend Claude-SDK events gain an epoch-ms `ts` field; live events self-stamp at construction via a dataclass `default_factory`, replayed events carry the real ISO timestamp read from the SDK `.jsonl`. The frontend reducer copies `ts` onto each block at creation time (never on streaming append), and a shared `BlockRow` gutter renders local `HH:MM` with a full-precision tooltip.

**Tech Stack:** Python dataclasses (FastAPI backend) · React + TypeScript + Tailwind (frontend) · pytest · vitest.

## Global Constraints

- Never add a NOT NULL DB column — but this feature adds **no** DB column at all (transcripts are file-based, replayed from `.jsonl`).
- Backend tests: run via the prebuilt env: `<main-checkout>/.venv/bin/python -m pytest server/tests/…`. The main checkout for this worktree is `/home/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/03d63b97` (additional working dir) or the deploy worktree; use whichever has `.venv`.
- Web tests run in `web/`: `node_modules/.bin/vitest run`, `node_modules/.bin/tsc --noEmit`, `node_modules/.bin/eslint`. Symlink `web/node_modules` from the deploy worktree if absent (do NOT use `npx`).
- `ts` on the wire is epoch **milliseconds** (float), browser-local at render. Field is **optional** on every frontend type so legacy events and existing tests stay valid.
- Timestamp marks **when a block first appeared**; streaming `textDelta`/`thinkingDelta` appends and a later `toolResult` must NOT overwrite it.
- Stay within scope: no per-turn headers, no relative times, no settings toggle, no gutters in nested sub-agent threads.

---

### Task 1: Backend — `ts` field on block-bearing events

**Files:**
- Modify: `server/sessions/events.py`
- Test: `server/tests/test_events.py`

**Interfaces:**
- Produces: each block-bearing event dataclass (`UserMessage`, `TextDelta`, `ThinkingDelta`, `ToolCall`, `ToolResult`, `PermissionRequest`, `Question`, `ErrorEvent`) gains `ts: float | None = field(default_factory=_now_ms)` and emits `"ts": self.ts` in `to_wire()`. Module-level `_now_ms() -> float` returns `time.time() * 1000`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_events.py`:

```python
import server.sessions.events as ev


def test_block_events_carry_ts_on_wire():
    for e in [
        ev.UserMessage(turn_id="t", text="hi"),
        ev.TextDelta("t", "hi"),
        ev.ThinkingDelta("t", "mm"),
        ev.ToolCall("t", "id", "Bash", ev.tool_kind("Bash"), {}),
        ev.ToolResult("id", True, {}),
        ev.PermissionRequest("p", "t", "Bash", ev.tool_kind("Bash"), {}),
        ev.Question("q", "t", "h", "q?", []),
        ev.ErrorEvent("code", "msg"),
    ]:
        wire = e.to_wire()
        assert "ts" in wire, f"{type(e).__name__} missing ts"
        assert isinstance(wire["ts"], float) and wire["ts"] > 0


def test_replay_can_override_ts():
    # Replay passes an explicit ts (may be None when the line lacks a timestamp).
    assert ev.TextDelta("t", "hi", ts=None).to_wire()["ts"] is None
    assert ev.TextDelta("t", "hi", ts=123.0).to_wire()["ts"] == 123.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<venv>/bin/python -m pytest server/tests/test_events.py::test_block_events_carry_ts_on_wire -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'ts'` or `KeyError: 'ts'`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/events.py`, update the imports (top of file):

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time


def _now_ms() -> float:
    return time.time() * 1000
```

Then add `ts: float | None = field(default_factory=_now_ms)` as the LAST field of each block-bearing dataclass and `"ts": self.ts` to its `to_wire()`. Example for `TextDelta` (apply the same pattern to `UserMessage`, `ThinkingDelta`, `ToolCall`, `ToolResult`, `PermissionRequest`, `Question`, `ErrorEvent`):

```python
@dataclass
class TextDelta:
    turn_id: str; text: str; src_uuid: str | None = None; parent_tool_use_id: str | None = None
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "textDelta", "turnId": self.turn_id, "text": self.text,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id,
                               "ts": self.ts}
```

Leave non-block events (`TurnStart`, `TurnEnd`, `SessionMeta`, `Title`, `Status`, `Rotated`, `RateLimit`, `TurnFailure`, `ReplayComplete`, `QueueUpdate`, `Suggestions`) untouched.

- [ ] **Step 4: Run tests to verify they pass**

Run: `<venv>/bin/python -m pytest server/tests/test_events.py -v`
Expected: PASS (new tests + all pre-existing event tests still green).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/tests/test_events.py
git commit -m "feat(events): add epoch-ms ts to block-bearing session events"
```

---

### Task 2: Backend — replay reads the real transcript timestamp

**Files:**
- Modify: `server/sessions/replay.py`
- Test: `server/tests/test_replay_ts.py` (create)

**Interfaces:**
- Consumes: the `ts=` keyword on events from Task 1.
- Produces: `_ts_ms(o: dict) -> float | None` parsing a line's ISO-8601 `timestamp` to epoch ms; every block-bearing event built in `_parse` receives `ts=_ts_ms(o)`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_replay_ts.py`:

```python
import json
from server.sessions import replay


def _write(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines))
    return str(p)


def test_replay_attaches_iso_timestamp_as_ms(tmp_path):
    path = _write(tmp_path, [
        {"type": "user", "uuid": "u1", "timestamp": "2026-06-23T12:00:00.000Z",
         "message": {"content": "hello"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-06-23T12:00:01.500Z",
         "message": {"model": "claude", "content": [{"type": "text", "text": "hi"}]}},
    ])
    out = replay._parse(path, max_turns=20)
    by = {type(e).__name__: e for e in out}
    assert by["UserMessage"].ts == 1782216000000.0          # 2026-06-23T12:00:00Z (UTC)
    assert by["TextDelta"].ts == 1782216001500.0            # +1.5s


def test_replay_missing_timestamp_is_none(tmp_path):
    path = _write(tmp_path, [
        {"type": "user", "uuid": "u1", "message": {"content": "hello"}},
    ])
    out = replay._parse(path, max_turns=20)
    um = next(e for e in out if type(e).__name__ == "UserMessage")
    assert um.ts is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<venv>/bin/python -m pytest server/tests/test_replay_ts.py -v`
Expected: FAIL — `AttributeError`/`AssertionError` (events built without `ts`, so `ts` defaults to "now", not the transcript time).

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/replay.py`, add near the top (after imports):

```python
from datetime import datetime


def _ts_ms(o: dict) -> float | None:
    raw = o.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None
```

Inside `_parse`, after `uuid = o.get("uuid")`, add `ts = _ts_ms(o)`. Then pass `ts=ts` to every block-bearing event constructed in the loop:

```python
out.append(ev.UserMessage(turn_id=uuid, text=text, src_uuid=uuid, images=imgs or None, ts=ts))
...
out.append(ev.ToolResult(r["tool_use_id"], not r.get("is_error", False),
                         {"content": r.get("content")}, uuid, ts=ts))
...
out.append(ev.TextDelta(uuid, b.get("text", ""), uuid, ts=ts))
out.append(ev.ThinkingDelta(uuid, b.get("thinking", ""), uuid, ts=ts))
...
out.append(ev.Question(question_id=..., turn_id=uuid, header=..., question=...,
                       options=opts, multi_select=..., ts=ts))
...
out.append(ev.ToolCall(uuid, b.get("id", ""), b.get("name", ""),
                       ev.tool_kind(b.get("name", "")), b.get("input", {}), uuid, ts=ts))
```

`TurnStart`/`TurnEnd`/`SessionMeta` stay unchanged (no `ts`). Verify the exact positional argument order against the current dataclass signatures before adding the keyword.

- [ ] **Step 4: Run tests to verify they pass**

Run: `<venv>/bin/python -m pytest server/tests/test_replay_ts.py server/tests/ -k replay -v`
Expected: PASS (new tests + existing replay tests green).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/replay.py server/tests/test_replay_ts.py
git commit -m "feat(replay): carry the transcript's real timestamp onto replayed blocks"
```

---

### Task 3: Frontend — `ts` on events + stamp blocks at creation

**Files:**
- Modify: `web/src/session/events.ts`
- Modify: `web/src/session/reducer.ts`
- Test: `web/src/session/reducer.test.ts`

**Interfaces:**
- Consumes: the wire `ts` field from Tasks 1–2.
- Produces: `ts?: number` on `EvUser`, `EvTextDelta`, `EvThinkingDelta`, `EvToolCall`, `EvToolResult`, `EvPermissionRequest`, `EvQuestion`, `EvError`; `ts?: number` on every `Block` variant; `userTurnBlocks(turnId, text, images?, ts?)` stamps the blocks it builds.

- [ ] **Step 1: Write the failing test**

Add to `web/src/session/reducer.test.ts`:

```ts
import { transcriptReducer, initialTranscript } from './reducer'

test('block ts is stamped at creation and not overwritten by streaming append', () => {
  let s = transcriptReducer(initialTranscript, { type: 'turnStart', turnId: 'a1' })
  s = transcriptReducer(s, { type: 'textDelta', turnId: 'a1', text: 'Hel', ts: 1000 })
  s = transcriptReducer(s, { type: 'textDelta', turnId: 'a1', text: 'lo', ts: 2000 })
  const block = s.turns.at(-1)!.blocks[0]
  expect(block.text).toBe('Hello')
  expect(block.ts).toBe(1000) // first delta wins
})

test('tool block keeps its create-time ts after toolResult', () => {
  let s = transcriptReducer(initialTranscript, { type: 'turnStart', turnId: 'a1' })
  s = transcriptReducer(s, { type: 'toolCall', turnId: 'a1', toolId: 'x', name: 'Bash', toolKind: 'bash', input: {}, ts: 500 })
  s = transcriptReducer(s, { type: 'toolResult', toolId: 'x', ok: true, payload: {}, ts: 999 })
  const block = s.turns.at(-1)!.blocks.find((b) => b.kind === 'tool')!
  expect(block.ts).toBe(500)
})

test('user block carries ts', () => {
  const s = transcriptReducer(initialTranscript, { type: 'user', turnId: 'u1', text: 'hi', ts: 42 })
  expect(s.turns[0].blocks[0].ts).toBe(42)
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `node_modules/.bin/vitest run src/session/reducer.test.ts`
Expected: FAIL — `ts` is `undefined` on the blocks.

- [ ] **Step 3: Write minimal implementation**

In `web/src/session/events.ts`, add `ts?: number` to the end of each interface:
`EvUser`, `EvTextDelta`, `EvThinkingDelta`, `EvToolCall`, `EvToolResult`,
`EvPermissionRequest`, `EvQuestion`, `EvError`. Example:

```ts
export interface EvTextDelta { type: 'textDelta'; turnId: string; text: string; srcUuid?: string | null; parentToolUseId?: string | null; ts?: number }
```

In `web/src/session/reducer.ts`:

1. Add `ts?: number` to every variant of the `Block` union (lines 10–18).
2. `userTurnBlocks` — add a `ts?: number` param and apply it:

```ts
function userTurnBlocks(turnId: string, text: string, images?: EvUser['images'], ts?: number): Block[] {
  const blocks: Block[] = [{ kind: 'text', id: `${turnId}:u`, text, ts }]
  if (images && images.length) blocks.push({ kind: 'image', id: `${turnId}:img`, images, ts })
  return blocks
}
```

Update its three call sites to pass `ev.ts` (both `user` cases in `applyToThread` and `transcriptReducer`, plus the optimistic-adoption path).

3. Stamp `ts` on each *new-block* creation, in BOTH `applyToThread` and the main switch:
   - `textDelta` / `thinkingDelta`: in the **"open a new block"** branch only, add `ts: ev.ts` to the new block literal. Do NOT touch the append branch.
   - `toolCall`: `const newBlock: Block = { kind: 'tool', id: toolId, toolId, name, toolKind, input, ts: ev.ts }`.
   - `toolResult`: leave unchanged (it spreads the existing block, preserving `ts`).
   - `permissionRequest`: `{ kind: 'permission', ..., plan, ts: ev.ts }`.
   - `question`: `{ kind: 'question', ..., live: !state.replaying, ts: ev.ts }`.
   - inline `error`: `{ kind: 'error', ..., resetsAt: ev.resetsAt, ts: ev.ts }`.

- [ ] **Step 4: Run checks to verify pass**

Run (in `web/`):
`node_modules/.bin/vitest run src/session/reducer.test.ts && node_modules/.bin/tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/events.ts web/src/session/reducer.ts web/src/session/reducer.test.ts
git commit -m "feat(transcript): stamp ts onto each block at creation time"
```

---

### Task 4: Frontend — render the timestamp gutter

**Files:**
- Create: `web/src/lib/formatBlockTime.ts`
- Create: `web/src/components/session/BlockRow.tsx`
- Modify: `web/src/components/session/Turn.tsx`
- Test: `web/src/lib/formatBlockTime.test.ts` (create), `web/src/components/session/BlockRow.test.tsx` (create)

**Interfaces:**
- Consumes: `Block.ts` from Task 3 (and `item.tools[0].ts` for tool groups).
- Produces: `formatBlockTime(ts?: number): { short: string; full: string }` (browser-local; empty strings when `ts` is absent). `<BlockRow ts?: number>` wrapping a content column with a left time gutter.

- [ ] **Step 0: Reuse check**

Run (in `web/`): `grep -rn "toLocaleTimeString\|Intl.DateTimeFormat" src/lib src/components/session` — if a shared local-time helper already exists, import it instead of writing a new formatter; otherwise create `formatBlockTime.ts` as below.

- [ ] **Step 1: Write the failing tests**

Create `web/src/lib/formatBlockTime.test.ts`:

```ts
import { formatBlockTime } from './formatBlockTime'

test('absent ts yields empty strings', () => {
  expect(formatBlockTime(undefined)).toEqual({ short: '', full: '' })
})

test('present ts yields HH:MM short and a longer full form', () => {
  const { short, full } = formatBlockTime(Date.UTC(2026, 5, 23, 14, 32, 7))
  expect(short).toMatch(/\d{1,2}:\d{2}/)
  expect(full.length).toBeGreaterThan(short.length)
})
```

Create `web/src/components/session/BlockRow.test.tsx`:

```tsx
import { render } from '@testing-library/react'
import { BlockRow } from './BlockRow'

test('renders a time element with title when ts present', () => {
  const { container } = render(<BlockRow ts={Date.UTC(2026, 5, 23, 14, 32, 7)}><span>hi</span></BlockRow>)
  const time = container.querySelector('time')!
  expect(time.textContent).toMatch(/\d{1,2}:\d{2}/)
  expect(time.getAttribute('title')).toBeTruthy()
})

test('absent ts renders an aligned empty gutter (no Invalid Date)', () => {
  const { container } = render(<BlockRow><span>hi</span></BlockRow>)
  expect(container.textContent).not.toMatch(/Invalid/)
  expect(container.querySelector('time')).not.toBeNull() // gutter slot still present
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `node_modules/.bin/vitest run src/lib/formatBlockTime.test.ts src/components/session/BlockRow.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write minimal implementation**

Create `web/src/lib/formatBlockTime.ts`:

```ts
/** Browser-local short (HH:MM) and full (HH:MM:SS + date) forms for a block's epoch-ms ts. */
export function formatBlockTime(ts?: number): { short: string; full: string } {
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return { short: '', full: '' }
  const d = new Date(ts)
  const short = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const full = d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'medium' })
  return { short, full }
}
```

Create `web/src/components/session/BlockRow.tsx`:

```tsx
import type { ReactNode } from 'react'
import { formatBlockTime } from '@/lib/formatBlockTime'

/** Wraps a transcript block in a row with a fixed-width left time gutter. */
export function BlockRow({ ts, children }: { ts?: number; children: ReactNode }) {
  const { short, full } = formatBlockTime(ts)
  return (
    <div className="flex gap-2">
      <time className="w-10 shrink-0 select-none pt-1 text-right text-[10px] leading-5 tabular-nums text-muted-foreground" title={full || undefined}>
        {short}
      </time>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}
```

In `web/src/components/session/Turn.tsx`, import `BlockRow` and wrap each rendered `item`. Capture the representative `ts` before the `switch`, and wrap both the `toolGroup` return and the per-block returns:

```tsx
const rowTs = item.kind === 'toolGroup' ? item.tools[0]?.ts : item.block.ts
// ...wrap each returned element:
return <BlockRow key={key} ts={rowTs}>{element}</BlockRow>
```

Keep the existing `key` on the `BlockRow` (move the `key` prop up to the wrapper; the inner element no longer needs it). Preserve all existing props passed to the inner components.

- [ ] **Step 4: Run checks to verify pass**

Run (in `web/`):
`node_modules/.bin/vitest run src/lib/formatBlockTime.test.ts src/components/session/BlockRow.test.tsx && node_modules/.bin/tsc --noEmit && node_modules/.bin/eslint src/components/session/Turn.tsx src/components/session/BlockRow.tsx src/lib/formatBlockTime.ts`
Expected: PASS, no type/lint errors.

- [ ] **Step 5: Run the full transcript test suite (regression)**

Run (in `web/`): `node_modules/.bin/vitest run src/session src/components/session`
Expected: PASS — Turn/transcript snapshots and reducer tests green (update any snapshot that legitimately changed due to the new gutter wrapper).

- [ ] **Step 6: Commit**

```bash
git add web/src/lib/formatBlockTime.ts web/src/lib/formatBlockTime.test.ts web/src/components/session/BlockRow.tsx web/src/components/session/BlockRow.test.tsx web/src/components/session/Turn.tsx
git commit -m "feat(transcript): render a per-block timestamp gutter"
```

---

## Self-Review

**Spec coverage:** §1 backend events → Task 1; §2 replay → Task 2; §3 frontend types + §4 reducer → Task 3; §5 rendering → Task 4; §6 tests → distributed across all tasks. No gaps.

**Placeholder scan:** No TBD/TODO; every code step shows concrete code. `<venv>` and the "verify positional order" notes are operational instructions, not placeholders.

**Type consistency:** `ts: float | None` (Python) ↔ `ts?: number` (TS) ↔ wire `ts`; `formatBlockTime` and `BlockRow` signatures match between Task 4's definition and its tests; `userTurnBlocks` 4th param `ts?` matches all call sites.
