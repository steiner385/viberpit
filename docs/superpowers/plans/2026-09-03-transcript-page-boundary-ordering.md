# Transcript Page-Boundary Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the structured session view from rendering user prompts out of chronological order at history-page boundaries, and give the transcript pipeline an ordering key plus telemetry so the same class of bug is detected instead of rediscovered.

**Architecture:** The disk reader (`server/sessions/replay.py`) already numbers assistant turns in a stable "turn space" used by the `?before=` page cursor; this plan (1) makes the client's page merge treat the *older* page as authoritative for position, which fixes the observed bug with no wire change, (2) stamps that turn ordinal onto `turnStart` events so the client can assert order after every merge, and (3) adds telemetry at the two places the reducer fabricates position (page merge, tail-append of unknown turns).

**Tech Stack:** Python 3 / FastAPI (server), React 18 + TypeScript + vitest (web), `@tanstack/react-virtual` (unchanged).

**Spec:** This document — the "Findings" section below is the spec. There is no separate design doc; the investigation was done against production session `888ad2bf-154f-477c-afd2-c1e1878e7d1a` on 2026-09-03.

## Global Constraints

- Constitution `.specify/memory/constitution.md` v1.1.0 applies: TDD (V), single source of truth / no dead code (VI), verify against real data (III).
- The `lint-and-test` gate is four steps: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Run all four before pushing.
- Web gate (run from `web/`): `vitest run` (FULL suite, not changed files only), `tsc --noEmit`, `eslint`.
- Run server tests with `/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest` and `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`, with every `CCSWITCH_SESSIOND_*` and `CCSWITCH_DAEMON_ROLE` unset.
- Never `git push --force`; branch off `origin/master` after `git fetch`.
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Do not use `cd web` persistently in Bash; use `(cd web && …)` subshells.

---

## Findings (the spec)

### Symptom

On `/sessions/888ad2bf-…` the conversation view shows the user prompt **"Start Phase B" (sent 4:34 PM)** between two assistant messages from **7:06 PM**, **"The daemon was restarted (deploy)…" (7:19 PM)** after a 7:29 PM message, and **"Set up the GH Projects hierarchy…" (8:03 PM)** after an 8:05 PM message. The user described this as "phasing" / rendering out of order.

### Evidence (all reproducible)

1. Reading the reducer's `turns` array from React state (not the DOM) on the live page gave 246 turns, zero duplicate ids, and **exactly three inversions** — the three user prompts above. Every other turn was chronological.
2. Each inversion sits **exactly at a history page boundary**. The page fetched as `GET /api/sessions/{id}/transcript?before=849&limit=20` (turns 829–848, content 7:06 PM–7:19 PM) begins with a `user` event for "Start Phase B" carrying its original `ts` (4:34 PM) and `srcUuid`. The pages `before=829` and `before=809` begin with the same event. Page `before=889` and `before=869` both begin with "The daemon was restarted…". The initial WebSocket attach window (turns 909–929) begins with "Set up the GH Projects hierarchy…", whose real position is inside page `before=909`.
3. Pages themselves are contiguous and non-overlapping (turn-id sets are disjoint; timestamps abut).
4. The raw claude JSONL has ~100 timestamp inversions per long session, all benign bookkeeping (`stop_hook_summary` written late with the previous turn's timestamp, `last-prompt` records repeating the prompt text). `replay.py` uses **file order**, which is correct, and emits nothing for those records. The raw file is **not** the cause.

### Mechanism

- `server/sessions/replay.py:_window` (lines 484–503) prepends the nearest preceding `UserMessage` to any window whose head lands mid-logical-turn ("owning prompt"), so a page of tool-heavy assistant entries always travels with its prompt. It is emitted as an ordinary `user` event — indistinguishable from an in-window prompt. The attach window and every scroll-up page in a mega-turn get the same prefix.
- `web/src/session/reducer.ts:_prependHistory` (lines 874–887) prepends the fetched page as a block and dedupes by turn id, **keeping the copy already on screen**. The on-screen copy is the *prefix* from the previously loaded (newer) page, sitting at the boundary between two pages. When the page containing the prompt's real position arrives, its copy is dropped, and the prefix stays at the boundary forever.
- The client has **no global ordering key**: render order is arrival order; `ts` is only used for the gutter label; the server's turn ordinal exists only as a page cursor and is never stamped on events.

### Five whys

1. *Why does "Start Phase B" render at 7:06 PM?* — `_prependHistory` keeps the first-seen copy of a turn id; the first-seen copy was the owning-prompt prefix of page 829–848, inserted at that page's top.
2. *Why does a page carry a prompt that belongs hours earlier?* — `_window` prepends the owning `UserMessage` to any window whose head lands mid-logical-turn, as a plain `user` event with its original timestamp and uuid.
3. *Why can't the client put it in the right place?* — Nothing on the wire says where a turn belongs; the client only knows arrival order and assumes each fetched page is strictly older than everything on screen, and that duplicates are already-correct.
4. *Why is there no ordering key?* — The event stream was designed append-only for live streaming; history paging, attach replay, live-extras re-emits, and sub-agent reconstruction were each bolted on with their own positional assumption ("windows are disjoint", "position is irrelevant", "prepend the owner"). Each holds alone; composed, they conflict.
5. *Why wasn't it caught?* — Reducer and replay tests exercise each layer with synthetic windows that never include the owner prefix and never merge more than one page; no test or runtime check asserts chronological monotonicity across merges; the bug only appears in sessions with >20 assistant entries per prompt (long autonomous sessions), which tests never model.

### Fix strategy

| # | Change | Fixes | Layer |
|---|--------|-------|-------|
| 1 | Older page is authoritative for position in `_prependHistory` | The three observed inversions, and any future duplicate-across-pages shape | web |
| 2 | Stamp `turnIndex` on `turnStart` from the disk reader | Gives the client a ground-truth ordering key | server |
| 3 | `Turn.ordinal` + `findOrderViolations` + `transcript.order.violation` telemetry after every page merge / replay | Detects regressions in production logs instead of by eye | web |
| 4 | `transcript.turn.fabricated` telemetry when a block event arrives for an unknown turn | Sibling sweep: the other tail-fabrication site (attach re-emits of pending asks/errors for turns outside the replay window) | web |
| 5 | Verify against the real session after deploy | Constitution III | ops |

Not in scope (deliberately): rendering the owning-prompt prefix differently, sorting turns by ordinal (arrival order is correct for live streams and the merge fix makes paged order correct), and any change to the JSONL reader's file-order semantics.

---

## File Structure

- Modify `web/src/session/reducer.ts` — `_prependHistory` merge rule (Task 1), `Turn.ordinal` + `findOrderViolations` + telemetry (Task 3), `ensureAssistantTurn` telemetry (Task 4). One file owns event→turn translation (constitution VI); do not add a second builder.
- Modify `web/src/session/events.ts` — `EvTurnStart.turnIndex` (Task 3).
- Modify `web/src/session/reducer.history.test.ts` — page-merge regression tests (Task 1), ordinal/violation tests (Task 3).
- Create `web/src/session/__tests__/reducer.fabricated.test.ts` — Task 4.
- Modify `server/sessions/events.py` — `TurnStart.turn_index` (Task 2).
- Modify `server/sessions/replay.py` — stamp ordinals in `_window` (Task 2).
- Modify `server/tests/test_replay_window.py` — ordinal tests (Task 2).

---

### Task 1: Older page is authoritative in `_prependHistory`

**Files:**
- Modify: `web/src/session/reducer.ts:874-887`
- Test: `web/src/session/reducer.history.test.ts`

**Interfaces:**
- Consumes: `buildTurnsFromEvents(events): Turn[]` (existing, `reducer.ts:1012`), `Turn { id, role, blocks }`.
- Produces: no new exports. Behaviour: after `_prependHistory`, any turn id present in the incoming page appears exactly once, at the incoming page's position.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/session/reducer.history.test.ts` inside the existing `describe('history paging reducer', …)` block. The fixtures mirror the real pages captured on 2026-09-03: an attach window prefixed with the owning prompt whose real position is in the next-older page, and two consecutive mega-turn pages both prefixed with the same prompt.

```ts
  // ── page-boundary ordering (session 888ad2bf, 2026-09-03) ────────────────────
  // The server prepends a window's OWNING prompt to any page whose head lands
  // mid-logical-turn. The same prompt id therefore arrives once per page until the
  // page holding its real position loads. The older page must win.

  const settled = (turns: SessionEvent[]) => ({
    ...initialTranscript, replaying: false, earliestTurn: 909,
    turns: buildTurnsFromEvents(turns),
  })

  it('moves a boundary-prefixed prompt to its real position when the older page holding it arrives', () => {
    // Attach window (turns 909..): owner prefix U_gh (real position is in page 889..908).
    const attachWindow: SessionEvent[] = [
      { type: 'user', turnId: 'U_gh', text: 'Set up the GH Projects hierarchy', ts: 3 },
      { type: 'turnStart', turnId: 'a909' }, { type: 'textDelta', turnId: 'a909', text: 'x', ts: 5 }, { type: 'turnEnd', turnId: 'a909' },
    ]
    // Page before=909 (turns 889..908): prefix U_ready, then a889, then U_gh at its REAL position, then a908.
    const page909: SessionEvent[] = [
      { type: 'user', turnId: 'U_ready', text: 'are we ready to dogfood this?', ts: 1 },
      { type: 'turnStart', turnId: 'a889' }, { type: 'textDelta', turnId: 'a889', text: 'y', ts: 2 }, { type: 'turnEnd', turnId: 'a889' },
      { type: 'user', turnId: 'U_gh', text: 'Set up the GH Projects hierarchy', ts: 3 },
      { type: 'turnStart', turnId: 'a908' }, { type: 'textDelta', turnId: 'a908', text: 'z', ts: 4 }, { type: 'turnEnd', turnId: 'a908' },
    ]
    let s = settled(attachWindow)
    expect(s.turns.map((t) => t.id)).toEqual(['U_gh', 'a909'])
    s = transcriptReducer(s, { type: '_prependHistory', events: page909, earliestTurn: 889 } as never)
    expect(s.turns.map((t) => t.id)).toEqual(['U_ready', 'a889', 'U_gh', 'a908', 'a909'])
    expect(s.turns.filter((t) => t.id === 'U_gh')).toHaveLength(1)
  })

  it('keeps a repeated owner prefix at the top of the OLDEST loaded page, not at a page boundary', () => {
    // Mega-turn: pages 829..848 and 809..828 are both prefixed with U_phase.
    const page849: SessionEvent[] = [
      { type: 'user', turnId: 'U_phase', text: 'Start Phase B', ts: 1 },
      { type: 'turnStart', turnId: 'a829' }, { type: 'textDelta', turnId: 'a829', text: 'b', ts: 20 }, { type: 'turnEnd', turnId: 'a829' },
    ]
    const page829: SessionEvent[] = [
      { type: 'user', turnId: 'U_phase', text: 'Start Phase B', ts: 1 },
      { type: 'turnStart', turnId: 'a809' }, { type: 'textDelta', turnId: 'a809', text: 'a', ts: 10 }, { type: 'turnEnd', turnId: 'a809' },
    ]
    let s = settled([{ type: 'turnStart', turnId: 'a849' }, { type: 'turnEnd', turnId: 'a849' }])
    s = transcriptReducer(s, { type: '_prependHistory', events: page849, earliestTurn: 829 } as never)
    expect(s.turns.map((t) => t.id)).toEqual(['U_phase', 'a829', 'a849'])
    s = transcriptReducer(s, { type: '_prependHistory', events: page829, earliestTurn: 809 } as never)
    // Before the fix this was ['a809', 'U_phase', 'a829', 'a849'] — the prompt pinned at the boundary.
    expect(s.turns.map((t) => t.id)).toEqual(['U_phase', 'a809', 'a829', 'a849'])
  })

  it('leaves the existing turns untouched when the page has no overlap', () => {
    let s = settled([{ type: 'turnStart', turnId: 'a-cur' }, { type: 'turnEnd', turnId: 'a-cur' }])
    s = transcriptReducer(s, { type: '_prependHistory', events: olderTurn, earliestTurn: 4 } as never)
    expect(s.turns.map((t) => t.id)).toEqual(['u-old', 'a-old', 'a-cur'])
  })
```

- [ ] **Step 2: Run the tests to verify the two new merge tests fail**

Run: `(cd web && npx vitest run src/session/reducer.history.test.ts)`
Expected: FAIL. The old rule drops the incoming `U_gh` because the id already exists, so "moves a boundary-prefixed prompt…" yields `['U_ready', 'a889', 'a908', 'U_gh', 'a909']` (prompt still after `a908`). "keeps a repeated owner prefix…" yields `['a809', 'U_phase', 'a829', 'a849']` (prompt pinned at the page boundary). The third test passes already.

- [ ] **Step 3: Implement the merge rule**

Replace the body of `case '_prependHistory'` in `web/src/session/reducer.ts` (lines 874–887) with:

```ts
    case '_prependHistory': {
      // A heal's replayReset cleared `turns` and the replay is rebuilding them from a
      // window that may not adjoin the one this (pre-heal, in-flight) page was fetched
      // against — prepending it would splice disjoint windows with a silent hole between
      // them. Drop the page; the scroll-up that wanted it re-fetches against the fresh
      // transcript once the replay settles.
      if (state.replaying) return { ...state, loadingOlder: false }
      const olderTurns = buildTurnsFromEvents(ev.events)
      // The OLDER page is authoritative for position. The server prepends a window's
      // owning UserMessage to any page (and to the attach window) whose head lands
      // mid-logical-turn, so the same prompt id arrives once per page until the page
      // holding its real position loads. A copy already on screen therefore sits at a
      // page boundary, not where the prompt was sent: drop it and let the incoming page
      // place it. (Keeping the on-screen copy pinned "Start Phase B" hours after its real
      // position — the page-boundary ordering bug, session 888ad2bf, 2026-09-03.)
      const incoming = new Set(olderTurns.map((t) => t.id))
      const kept = state.turns.filter((t) => !incoming.has(t.id))
      return { ...state, turns: [...olderTurns, ...kept],
               earliestTurn: ev.earliestTurn, loadingOlder: false }
    }
```

- [ ] **Step 4: Run the history tests, then the full web gate**

Run: `(cd web && npx vitest run src/session/reducer.history.test.ts)` — Expected: PASS (all).
Run: `(cd web && npx vitest run && npx tsc --noEmit && npx eslint .)` — Expected: all green. If `useSessionRuntime.reattach.test.ts` or `reducer.reassert.test.ts` fail, read the failure: they must not depend on the old "first copy wins" rule; if one does, the test encoded the bug and should be updated to the new expectation with a comment citing this plan.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/reducer.ts web/src/session/reducer.history.test.ts
git commit -F /tmp/commit-msg-task1.txt
```
where `/tmp/commit-msg-task1.txt` contains:
```
fix(web): older history page is authoritative when merging transcript pages

The server prepends a window's owning prompt to any page whose head lands
mid-logical-turn, so the same user turn id arrives once per page. The merge
kept the first-seen copy — the boundary prefix — and dropped the copy at the
prompt's real position, pinning prompts hours after they were sent.

Refs #1000

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

### Task 2: Stamp `turnIndex` on `turnStart` from the disk reader

**Files:**
- Modify: `server/sessions/events.py:29-33`
- Modify: `server/sessions/replay.py:455-513` (`_window`)
- Test: `server/tests/test_replay_window.py`

**Interfaces:**
- Produces: `ev.TurnStart.turn_index: int | None` (dataclass field, default `None`), wire key `"turnIndex"` on every `turnStart` frame (`null` for live-translated and sub-agent turns). Ordinal space: identical to `ReplayWindow.earliest_turn` / the `?before=` cursor — the first in-window `TurnStart` carries `turn_index == earliest_turn`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_replay_window.py`:

```python
@pytest.mark.asyncio
async def test_window_turnstarts_carry_turn_index_in_cursor_space(tmp_path):
    """Each in-window TurnStart is stamped with its ordinal in the same space the
    `?before=` cursor / `earliest_turn` index, so the client can verify that page
    merges keep turn order (the page-boundary ordering bug, session 888ad2bf)."""
    from server.sessions import events as ev
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, before=30, limit=20)
    idx = [e.turn_index for e in w.events if isinstance(e, ev.TurnStart)]
    assert idx == list(range(10, 30))
    assert idx[0] == w.earliest_turn


@pytest.mark.asyncio
async def test_window_turn_index_survives_owner_prefix_and_meta(tmp_path):
    """The owning-prompt prepend and the SessionMeta prepend shift list positions
    but must not shift ordinals."""
    from server.sessions import events as ev
    path = _write_megaturn_transcript(tmp_path, 3, 30)     # 33 assistant messages
    w = await replay_window(path, before=23, limit=10)     # m10..m19, prefixed with "big"
    starts = [e for e in w.events if isinstance(e, ev.TurnStart)]
    assert [e.turn_index for e in starts] == list(range(10, 20))
    assert isinstance(w.events[0], ev.SessionMeta)
    assert isinstance(w.events[1], ev.UserMessage)


def test_live_turnstart_has_no_turn_index():
    """Live-translated turns are not positioned in the disk turn space."""
    from server.sessions import events as ev
    assert ev.TurnStart("a").to_wire()["turnIndex"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_replay_window.py -k turn_index -v`
Expected: FAIL — `AttributeError: 'TurnStart' object has no attribute 'turn_index'` / `KeyError: 'turnIndex'`.

- [ ] **Step 3: Add the field**

In `server/sessions/events.py` replace the `TurnStart` dataclass (lines 29–33) with:

```python
@dataclass
class TurnStart:
    turn_id: str; src_uuid: str | None = None; parent_tool_use_id: str | None = None
    # Ordinal of this assistant entry in the transcript's turn space — the SAME space
    # `ReplayWindow.earliest_turn` and the `?before=` page cursor index. Stamped only by
    # the disk reader (`replay._window`); live-translated and sub-agent turns carry None.
    # The client uses it to verify page merges keep turn order (telemetry only).
    turn_index: int | None = None
    def to_wire(self): return {"type": "turnStart", "turnId": self.turn_id,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id,
                               "turnIndex": self.turn_index}
```

- [ ] **Step 4: Stamp ordinals in `_window`**

In `server/sessions/replay.py`, add `import dataclasses` to the module imports (keep the existing import block order), then in `_window` insert immediately after `window = out[slice_lo:slice_hi]` (line 483) and BEFORE the owner-prefix block:

```python
    # Stamp each in-window TurnStart with its ordinal in the cursor space. Done on the
    # slice (not `out`) so a stamped copy never leaks into the un-windowed `_parse`
    # consumers, and before the owner/meta prepends below shift list positions.
    for k, si in enumerate(starts[start:end]):
        window[si - slice_lo] = dataclasses.replace(out[si], turn_index=start + k)
```

- [ ] **Step 5: Run the tests, then the full server gate**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_replay_window.py -v` — Expected: PASS.
Run the four-step gate:
```bash
/home/tony/GitHub/ccswitch/.venv/bin/ruff check src/ server/
/home/tony/GitHub/ccswitch/.venv/bin/python -m mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/ -q 2>&1 | tee /tmp/gate-core.log | tail -3
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -q -x 2>&1 | tee /tmp/gate-server.log | tail -3
```
Expected: all green. `test_remote_transcript_rpc.py`, `test_transcript_endpoint.py` and `test_ws_inprogress_replay.py` exercise the wire shape; a failure there means an exact-dict comparison that must gain `"turnIndex": None` (or the stamped value).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/events.py server/sessions/replay.py server/tests/test_replay_window.py
git commit -F /tmp/commit-msg-task2.txt
```
where `/tmp/commit-msg-task2.txt` contains:
```
feat(replay): stamp turnIndex on windowed turnStart events

Gives the client a ground-truth ordinal in the same space as the ?before=
page cursor, so page merges can be checked for order instead of trusted.

Refs #1000

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

### Task 3: `Turn.ordinal`, `findOrderViolations`, and merge telemetry

**Files:**
- Modify: `web/src/session/events.ts:8`
- Modify: `web/src/session/reducer.ts:30` (`Turn`), `:525-539` (`turnStart`), `:874-887` (`_prependHistory`, as rewritten in Task 1), `:855-864` (`replayComplete`)
- Test: `web/src/session/reducer.history.test.ts`

**Interfaces:**
- Consumes: wire key `turnIndex` from Task 2.
- Produces: `Turn.ordinal?: number`; `export function findOrderViolations(turns: Turn[]): Array<[string, string]>` returning `[prevId, thisId]` pairs where `this.ordinal < prev.ordinal`; log line `transcript.order.violation count=<n> first=<prevId>><thisId> earliestTurn=<n> phase=<prepend|replay>`.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/session/reducer.history.test.ts` (add `import { vi } from 'vitest'` to the existing vitest import, `import { clientLogger } from '../obs/logger'`, and `findOrderViolations` to the reducer import):

```ts
  // ── ordinals + order telemetry ────────────────────────────────────────────────

  it('turnStart records the server turnIndex as the turn ordinal', () => {
    let s = { ...initialTranscript, replaying: false }
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'a7', turnIndex: 7 } as SessionEvent)
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'live' } as SessionEvent)
    expect(s.turns[0].ordinal).toBe(7)
    expect(s.turns[1].ordinal).toBeUndefined()
  })

  it('findOrderViolations reports adjacent ordinal-bearing turns that run backwards', () => {
    const t = (id: string, ordinal?: number) => ({ id, role: 'assistant' as const, blocks: [], ordinal })
    expect(findOrderViolations([t('a', 1), t('u'), t('b', 2), t('c', 3)])).toEqual([])
    expect(findOrderViolations([t('a', 5), t('b', 2), t('c', 9), t('d', 8)])).toEqual([['a', 'b'], ['c', 'd']])
  })

  it('_prependHistory logs transcript.order.violation when the merge breaks ordinal order', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    let s = { ...initialTranscript, replaying: false, earliestTurn: 20,
      turns: [{ id: 'a20', role: 'assistant' as const, blocks: [], ordinal: 20 }] }
    // A page that claims to be older but carries a HIGHER ordinal (server/cursor skew).
    s = transcriptReducer(s, { type: '_prependHistory', earliestTurn: 10,
      events: [{ type: 'turnStart', turnId: 'a25', turnIndex: 25 }, { type: 'turnEnd', turnId: 'a25' }] } as never)
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('transcript.order.violation count=1 first=a25>a20 earliestTurn=10 phase=prepend'))
    spy.mockRestore()
  })

  it('_prependHistory stays silent on an ordered merge', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    let s = { ...initialTranscript, replaying: false, earliestTurn: 20,
      turns: [{ id: 'a20', role: 'assistant' as const, blocks: [], ordinal: 20 }] }
    s = transcriptReducer(s, { type: '_prependHistory', earliestTurn: 19,
      events: [{ type: 'turnStart', turnId: 'a19', turnIndex: 19 }, { type: 'turnEnd', turnId: 'a19' }] } as never)
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `(cd web && npx vitest run src/session/reducer.history.test.ts)`
Expected: FAIL — `findOrderViolations` is not exported; `ordinal` undefined; no warn call.

- [ ] **Step 3: Extend the wire type and `Turn`**

In `web/src/session/events.ts` line 8:
```ts
export interface EvTurnStart     { type: 'turnStart';     turnId: string; srcUuid?: string | null; parentToolUseId?: string | null; turnIndex?: number | null }
```
In `web/src/session/reducer.ts` line 30:
```ts
// `ordinal` is the server's turn index in the `?before=` cursor space (disk replay only;
// live turns have none). It is NOT used to order rendering — arrival order is — only to
// verify that page merges kept order (transcript.order.violation telemetry).
export interface Turn { id: string; role: 'user' | 'assistant'; blocks: Block[]; model?: string | null; ordinal?: number }
```

- [ ] **Step 4: Record the ordinal on `turnStart`**

In the `case 'turnStart'` block (reducer.ts ~line 533), replace the turn literal:
```ts
      const ordinal = ev.turnIndex ?? undefined
      return {
        ...state,
        turns: [...state.turns, { id: turnId, role: 'assistant', blocks: [], ...(ordinal !== undefined ? { ordinal } : {}) }],
        openBlockId: null,
        latestSuggestions: [],
      }
```

- [ ] **Step 5: Add `findOrderViolations` and the telemetry**

Add near `findTurnIdx` (reducer.ts ~line 208):
```ts
/**
 * Pairs [prevId, thisId] of ordinal-bearing turns whose server ordinals run backwards.
 * Turns without an ordinal (live, user, tool-result-only) are skipped, so a violation
 * can only come from a merge that placed a disk-replayed turn out of cursor order.
 */
export function findOrderViolations(turns: Turn[]): Array<[string, string]> {
  const out: Array<[string, string]> = []
  let prev: Turn | null = null
  for (const t of turns) {
    if (t.ordinal === undefined) continue
    if (prev !== null && t.ordinal < (prev.ordinal as number)) out.push([prev.id, t.id])
    prev = t
  }
  return out
}

function warnIfOutOfOrder(turns: Turn[], earliestTurn: number, phase: 'prepend' | 'replay'): void {
  const bad = findOrderViolations(turns)
  if (bad.length === 0) return
  clientLogger.warn(`transcript.order.violation count=${bad.length} first=${bad[0][0]}>${bad[0][1]} earliestTurn=${earliestTurn} phase=${phase}`)
}
```
In `case '_prependHistory'` (Task 1's version), replace the `return` with:
```ts
      const turns = [...olderTurns, ...kept]
      warnIfOutOfOrder(turns, ev.earliestTurn, 'prepend')
      return { ...state, turns, earliestTurn: ev.earliestTurn, loadingOlder: false }
```
In `case 'replayComplete'`, immediately before its final `return` (the one that sets `replaying: false`, ~line 859), add:
```ts
      warnIfOutOfOrder(turns, ev.earliestTurn ?? state.earliestTurn, 'replay')
```
(`turns` is the local already built there by the re-assert logic; if the variable in that block has a different name, use it — do not compute a second copy.)

- [ ] **Step 6: Run the tests, then the full web gate**

Run: `(cd web && npx vitest run src/session/reducer.history.test.ts)` — Expected: PASS.
Run: `(cd web && npx vitest run && npx tsc --noEmit && npx eslint .)` — Expected: green. `reducer.property.test.ts` builds random event streams; if it now logs violations it is because random `turnIndex` values are generated — that test constructs events without `turnIndex`, so it should stay silent; if not, the fixture generator must not emit `turnIndex`.

- [ ] **Step 7: Commit**

```bash
git add web/src/session/events.ts web/src/session/reducer.ts web/src/session/reducer.history.test.ts
git commit -F /tmp/commit-msg-task3.txt
```
where `/tmp/commit-msg-task3.txt` contains:
```
feat(web): record turn ordinals and log transcript.order.violation on bad merges

Refs #1000

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

### Task 4: Telemetry for tail-fabricated turns (sibling sweep)

**Files:**
- Modify: `web/src/session/reducer.ts:280-284` (`ensureAssistantTurn`) and its 10 call sites (`:334, :356, :378, :544, :578, :612, :659, :687, :897`, plus any added since)
- Create: `web/src/session/__tests__/reducer.fabricated.test.ts`

**Interfaces:**
- Produces: log line `transcript.turn.fabricated type=<eventType> turnId=<id>` whenever a block event arrives for a turn no `turnStart` introduced. `ensureAssistantTurn(turns, turnId, evType: string)` — third argument is the triggering event's `type`.

Why this is the sibling: the attach path re-emits pending permissions/questions/errors with the turn id they belonged to (`server/bridge/ws.py:584-613`); when that turn is outside the 20-turn replay window the reducer fabricates an empty assistant turn **at the tail** — the same "content lands at a boundary instead of its real position" shape as the bug above. Counting it in production tells us whether it needs the same fix before anyone reports it.

- [ ] **Step 1: Write the failing test**

Create `web/src/session/__tests__/reducer.fabricated.test.ts`:
```ts
import { describe, it, expect, vi } from 'vitest'
import { transcriptReducer, initialTranscript } from '../reducer'
import type { SessionEvent } from '../events'
import { clientLogger } from '../../obs/logger'

describe('transcript.turn.fabricated telemetry', () => {
  it('warns when a block event arrives for a turn no turnStart introduced', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s = transcriptReducer({ ...initialTranscript, replaying: false },
      { type: 'textDelta', turnId: 'ghost', text: 'late' } as SessionEvent)
    expect(s.turns.map((t) => t.id)).toEqual(['ghost'])   // behaviour unchanged: still appended
    expect(spy).toHaveBeenCalledWith('transcript.turn.fabricated type=textDelta turnId=ghost')
    spy.mockRestore()
  })

  it('stays silent when the turn exists', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    let s = transcriptReducer({ ...initialTranscript, replaying: false },
      { type: 'turnStart', turnId: 'a1' } as SessionEvent)
    s = transcriptReducer(s, { type: 'textDelta', turnId: 'a1', text: 'ok' } as SessionEvent)
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `(cd web && npx vitest run src/session/__tests__/reducer.fabricated.test.ts)`
Expected: FAIL — warn not called.

- [ ] **Step 3: Implement**

Replace `ensureAssistantTurn` (reducer.ts:280-284):
```ts
/**
 * Ensure a turn exists at turnId; if it doesn't, append an empty assistant
 * turn at the TAIL and count it. A block event with no preceding turnStart means the
 * frame reached us out of order (an attach re-emit of a pending ask/error whose turn
 * fell outside the replay window, a delta for an evicted turn) — the fabricated turn
 * may render far from where its content belongs. Returns [nextTurns, idx].
 */
function ensureAssistantTurn(turns: Turn[], turnId: string, evType: string): [Turn[], number] {
  const idx = findTurnIdx(turns, turnId)
  if (idx !== -1) return [turns, idx]
  clientLogger.warn(`transcript.turn.fabricated type=${evType} turnId=${turnId}`)
  return [[...turns, { id: turnId, role: 'assistant', blocks: [] }], turns.length]
}
```
Update every call site to pass the event type, e.g. `ensureAssistantTurn(state.turns, turnId, ev.type)` and, inside `applyToThread`, `ensureAssistantTurn(thread.turns, turnId, ev.type)`. `tsc` will list any site missed.

- [ ] **Step 4: Run the test, then the full web gate**

Run: `(cd web && npx vitest run src/session/__tests__/reducer.fabricated.test.ts)` — Expected: PASS.
Run: `(cd web && npx vitest run && npx tsc --noEmit && npx eslint .)` — Expected: green. Existing tests that dispatch deltas without a `turnStart` will now emit warn calls; that is acceptable noise unless a test asserts `warn` was **not** called — those tests should add a `turnStart` first rather than loosen the assertion.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/reducer.ts web/src/session/__tests__/reducer.fabricated.test.ts
git commit -F /tmp/commit-msg-task4.txt
```
where `/tmp/commit-msg-task4.txt` contains:
```
feat(web): log transcript.turn.fabricated when a block event has no turnStart

Sibling of the page-boundary ordering bug: the other place the reducer
invents a position (tail) for content whose real position it cannot know.

Refs #1000

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

### Task 5: Verify on the real session and land

**Files:** none (verification + PR).

- [ ] **Step 1: Run every gate once more on the branch head**

```bash
/home/tony/GitHub/ccswitch/.venv/bin/ruff check src/ server/
/home/tony/GitHub/ccswitch/.venv/bin/python -m mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/ -q | tail -2
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -q | tail -2
(cd web && npx vitest run && npx tsc --noEmit && npx eslint .)
```
Expected: all green.

- [ ] **Step 2: Open the PR**

```bash
git fetch origin
git push -u origin HEAD
gh pr create --title "fix(transcript): older history page is authoritative; turn ordinals + order telemetry" --body-file /tmp/pr-body.txt
gh pr merge <#> --squash --auto
```
`/tmp/pr-body.txt` must contain `Fixes #1000` and end with the Claude Code trailer.

- [ ] **Step 3: After deploy, verify against session 888ad2bf in the browser**

Open `https://dobby.tail0dd570.ts.net:8443/sessions/888ad2bf-154f-477c-afd2-c1e1878e7d1a`, scroll to the top six or more times so pages `909…809` load, then run in DevTools:

```js
(() => {
  const sc=[...document.querySelectorAll('div.flex-1.overflow-y-auto')].sort((a,b)=>b.scrollHeight-a.scrollHeight)[0];
  let f=sc[Object.keys(sc).find(k=>k.startsWith('__reactFiber$'))], turns=null;
  while(f){ if(Array.isArray(f.memoizedProps?.turns)){turns=f.memoizedProps.turns;break;} f=f.return; }
  const rows=turns.map((t,i)=>({i,id:t.id.slice(0,8),role:t.role,ts:Math.min(...t.blocks.map(b=>b.ts).filter(Boolean)),ord:t.ordinal}));
  const inv=rows.filter((r,i)=>i&&isFinite(r.ts)&&isFinite(rows[i-1].ts)&&r.ts<rows[i-1].ts-1000);
  return {turns:rows.length, inversions:inv, ordinals:rows.filter(r=>r.ord!==undefined).length};
})()
```
Expected: `inversions: []`, `ordinals > 0`. Then check the daemon log on dobby for the new lines:
```bash
ssh dobby 'journalctl -u ccswitch-daemon.service --since "1 hour ago" --no-pager | grep -c "transcript.order.violation"'
```
Expected: `0`. Any `transcript.turn.fabricated` count is informational — record it in the PR as the baseline for the sibling follow-up.

---

## Self-review

- **Spec coverage:** symptom (Task 1 tests reproduce the three real shapes), ordering key (Task 2), detection (Task 3), sibling sweep (Task 4), verification against real data (Task 5). No spec item without a task.
- **Placeholder scan:** `#1000` is intentionally filled at execution time from the GitHub issue linked in the PR that lands this plan; every other step has concrete code and commands.
- **Type consistency:** `turn_index` (Python) ↔ `turnIndex` (wire) ↔ `Turn.ordinal` (client); `findOrderViolations(turns): Array<[string,string]>`; `ensureAssistantTurn(turns, turnId, evType)` used identically in Tasks 3 and 4.
