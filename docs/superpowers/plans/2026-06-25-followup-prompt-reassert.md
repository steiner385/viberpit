# Follow-up Prompt Re-assert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a user's follow-up prompts from silently vanishing from the transcript when a WebSocket reconnect's `replayReset` wipe outruns the SDK's transcript-file write.

**Architecture:** Add a client-only "sent-but-not-yet-confirmed-on-disk" buffer to the transcript reducer. The reducer already tells a live echo from a disk replay via `state.replaying`. We track each sent prompt, remove it from the buffer when a *disk replay* (`replaying === true`) brings it back, and on `replayComplete` re-assert any buffered prompt not already present in `turns`. Purely additive client state; no server or wire change; the `replayReset` wipe stays intact so the duplication bug it fixed cannot return.

**Tech Stack:** React + TypeScript, Vitest. All changes in `web/src/session/`.

## Global Constraints

- Frontend only — no changes under `server/`, no wire-protocol change.
- Do NOT clear `pendingSent` / `pendingSentSeq` in the `replayReset` case (they must survive the wipe).
- Reducer stays pure (no `Date.now()` / `Math.random()` — ids come from the `pendingSentSeq` counter).
- Buffer cap: 20 entries (drop oldest).
- Real type gate is `npx tsc -b --force` (root `tsc --noEmit` checks nothing). Lint gate is `eslint` with `--max-warnings 0`. Run from `web/`.
- Run web tests from `web/`: `npx vitest run`.

---

### Task 1: Buffer state, `_trackSentPrompt` action, and `firstText` helper

**Files:**
- Modify: `web/src/session/reducer.ts` (state interface ~55-73, `initialTranscript` ~75-93, `LocalAction` union ~102-111, helpers ~113-118, and the action switch)
- Test: `web/src/session/reducer.reassert.test.ts` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `TranscriptState.pendingSent: { id: string; text: string }[]`
  - `TranscriptState.pendingSentSeq: number`
  - `LocalAction` variant `{ type: '_trackSentPrompt'; text: string }`
  - module-private `function firstText(turn: Turn): string` — first text-block text of a turn, or `''`.

- [ ] **Step 1: Write the failing tests**

Create `web/src/session/reducer.reassert.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

describe('transcriptReducer — _trackSentPrompt buffer', () => {
  it('adds a pending entry with a stable id and bumps the seq', () => {
    const s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    expect(s.pendingSent).toEqual([{ id: 'pending:0', text: 'hello' }])
    expect(s.pendingSentSeq).toBe(1)
    // tracking must not itself create a turn — the live echo renders it
    expect(s.turns).toHaveLength(0)
  })

  it('keeps insertion order and unique ids across multiple sends', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'a' })
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'b' })
    expect(s.pendingSent).toEqual([
      { id: 'pending:0', text: 'a' },
      { id: 'pending:1', text: 'b' },
    ])
    expect(s.pendingSentSeq).toBe(2)
  })

  it('caps the buffer at 20 entries, dropping the oldest', () => {
    let s = initialTranscript
    for (let i = 0; i < 25; i++) {
      s = transcriptReducer(s, { type: '_trackSentPrompt', text: `p${i}` })
    }
    expect(s.pendingSent).toHaveLength(20)
    expect(s.pendingSent[0]).toEqual({ id: 'pending:5', text: 'p5' })
    expect(s.pendingSent[19]).toEqual({ id: 'pending:24', text: 'p24' })
    expect(s.pendingSentSeq).toBe(25)
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `web/`): `npx vitest run src/session/reducer.reassert.test.ts`
Expected: FAIL — `pendingSent` is `undefined` / `_trackSentPrompt` not handled.

- [ ] **Step 3: Add the state fields and initial values**

In `web/src/session/reducer.ts`, add to the `TranscriptState` interface (after `condense` at ~72):

```ts
  condense: { active: boolean; expectedMs: number } | null
  pendingSent: { id: string; text: string }[]  // sent-but-unconfirmed-on-disk prompts (FIFO); survives replayReset
  pendingSentSeq: number                        // monotonic counter for stable synthetic ids
```

In `initialTranscript` (after `condense: null` at ~92):

```ts
  condense: null,
  pendingSent: [],
  pendingSentSeq: 0,
```

- [ ] **Step 4: Add the `LocalAction` variant**

In the `LocalAction` union (~102-111), add:

```ts
  | { type: '_setLoadingOlder'; value: boolean }
  | { type: '_trackSentPrompt'; text: string }
```

- [ ] **Step 5: Add the `firstText` helper**

Immediately after `findTurnIdx` (~116-118), add:

```ts
/** First text-block text of a turn (a user turn's prompt text), or '' if none. */
function firstText(turn: Turn): string {
  const b = turn.blocks.find((bl) => bl.kind === 'text')
  return b && b.kind === 'text' ? b.text : ''
}
```

- [ ] **Step 6: Handle `_trackSentPrompt` in the switch**

Add a case alongside the other `_`-prefixed local actions (e.g. just before `_clear` at ~666):

```ts
    case '_trackSentPrompt': {
      const MAX = 20
      const entry = { id: `pending:${state.pendingSentSeq}`, text: ev.text }
      const next = [...state.pendingSent, entry]
      const capped = next.length > MAX ? next.slice(next.length - MAX) : next
      return { ...state, pendingSent: capped, pendingSentSeq: state.pendingSentSeq + 1 }
    }
```

- [ ] **Step 7: Run tests to verify they pass**

Run (from `web/`): `npx vitest run src/session/reducer.reassert.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 8: Typecheck + lint**

Run (from `web/`): `npx tsc -b --force && npx eslint src/session/reducer.ts src/session/reducer.reassert.test.ts --max-warnings 0`
Expected: no errors. (`firstText` is referenced in Task 2; if `tsc`/eslint flags it as unused here, that is expected — proceed; it is consumed in the next task within the same PR. If your lint config errors on unused, complete Task 2 before the final lint.)

- [ ] **Step 9: Commit**

```bash
git add web/src/session/reducer.ts web/src/session/reducer.reassert.test.ts
git commit -m "feat(transcript): add sent-prompt buffer + _trackSentPrompt action

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Consume on disk replay + re-assert on `replayComplete`

**Files:**
- Modify: `web/src/session/reducer.ts` — the main-transcript `user` case (~285-313) and the `replayComplete` case (~549-553)
- Test: `web/src/session/reducer.reassert.test.ts` (extend)

**Interfaces:**
- Consumes: `pendingSent`, `pendingSentSeq`, `firstText`, `userTurnBlocks` (existing helper, ~147-151) from Task 1 / existing code.
- Produces: the user-visible behavior — buffer shrinks on disk-replay sightings; missing prompts re-asserted at `replayComplete`.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/session/reducer.reassert.test.ts`:

```ts
import type { SessionEvent } from './events'

const RESET: SessionEvent = { type: 'replayReset' }
const DONE: SessionEvent = { type: 'replayComplete' }
const userEv = (turnId: string, text: string): SessionEvent => ({ type: 'user', turnId, text })

function reduce(start: ReturnType<typeof transcriptReducer>, evs: (SessionEvent | { type: '_trackSentPrompt'; text: string })[]) {
  return evs.reduce(transcriptReducer, start)
}

function userTexts(s: ReturnType<typeof transcriptReducer>) {
  return s.turns.filter((t) => t.role === 'user').map((t) => {
    const b = t.blocks.find((bl) => bl.kind === 'text')
    return b && b.kind === 'text' ? b.text : ''
  })
}

describe('transcriptReducer — re-assert on replay', () => {
  it('a live echo (replaying=false) does NOT consume the buffer', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    // initialTranscript.replaying is true; close replay first so we are live
    s = transcriptReducer(s, DONE)            // replaying -> false
    s = transcriptReducer(s, userEv('live-1', 'hello'))  // live echo
    expect(s.pendingSent).toHaveLength(1)     // still buffered (not durable yet)
    expect(userTexts(s)).toEqual(['hello'])
  })

  it('a disk-replay user event (replaying=true) consumes the matching entry', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = reduce(s, [RESET, userEv('disk-1', 'hello'), DONE])  // RESET sets replaying=true
    expect(s.pendingSent).toHaveLength(0)
    expect(userTexts(s)).toEqual(['hello'])   // exactly one, from the replay
  })

  it('race repro: replayReset+replayComplete with no matching replay re-asserts the prompt', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = transcriptReducer(s, userEv('live-1', 'hello'))  // shown live
    // reconnect: wipe, replay brings back NOTHING (disk hasn't flushed), complete
    s = reduce(s, [RESET, DONE])
    expect(userTexts(s)).toEqual(['hello'])   // re-asserted, not lost
    expect(s.turns[s.turns.length - 1].id).toBe('pending:0')
    expect(s.pendingSent).toHaveLength(1)     // still unconfirmed on disk
  })

  it('heals on the next cycle once disk has the prompt (no duplicate)', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = reduce(s, [RESET, DONE])              // re-asserted (race)
    s = reduce(s, [RESET, userEv('disk-1', 'hello'), DONE])  // disk caught up
    expect(userTexts(s)).toEqual(['hello'])   // exactly one
    expect(s.pendingSent).toHaveLength(0)
  })

  it('no-wipe attach: replayComplete with a live echo present does not duplicate', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = transcriptReducer(s, userEv('live-1', 'hello'))  // live echo, turn present
    // empty-transcript attach: server sends replayComplete with NO replayReset
    s = transcriptReducer(s, DONE)
    expect(userTexts(s)).toEqual(['hello'])   // still exactly one
  })

  it('FIFO duplicates: two identical sends, one on disk, one re-asserted', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'yes' })
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'yes' })
    s = transcriptReducer(s, DONE)
    s = reduce(s, [RESET, userEv('disk-1', 'yes'), DONE])  // only one 'yes' on disk
    expect(userTexts(s)).toEqual(['yes', 'yes'])  // one replayed + one re-asserted
    expect(s.pendingSent).toHaveLength(1)         // one still unconfirmed
  })

  it('replayReset preserves the buffer', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'keep' })
    s = transcriptReducer(s, RESET)
    expect(s.pendingSent).toEqual([{ id: 'pending:0', text: 'keep' }])
    expect(s.pendingSentSeq).toBe(1)
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `web/`): `npx vitest run src/session/reducer.reassert.test.ts`
Expected: FAIL — buffer not consumed on replay; prompt not re-asserted on `replayComplete`.

- [ ] **Step 3: Consume the buffer on a disk-replay `user` event**

In the main-transcript `user` case (the `switch (ev.type)` block, ~285), compute the consumed buffer at the top of the case and include it in every return.

Replace the case body (~285-313) with:

```ts
    case 'user': {
      const { turnId, text } = ev
      const blocks = userTurnBlocks(turnId, text, ev.images, ev.ts)

      // A disk replay (replaying=true) re-emitting this prompt proves it is durably
      // on disk → drop the first matching pending entry. A live echo (replaying=false)
      // does NOT confirm durability, so it must not consume the buffer.
      let pendingSent = state.pendingSent
      if (state.replaying) {
        const pi = pendingSent.findIndex((p) => p.text === text)
        if (pi !== -1) pendingSent = [...pendingSent.slice(0, pi), ...pendingSent.slice(pi + 1)]
      }

      // Adopt an optimistic seed turn if present: re-key it in place to the real
      // server turnId instead of appending a duplicate. Unconditional adoption is
      // safe because a brand-new session starts with an empty replay — the first
      // user event is always the seeded prompt — and only one optimistic turn exists.
      const optIdx = findTurnIdx(state.turns, OPTIMISTIC_USER_ID)
      if (optIdx !== -1) {
        const nextTurns = replaceTurn(state.turns, optIdx, () => ({
          id: turnId,
          role: 'user' as const,
          blocks,
        }))
        return { ...state, turns: nextTurns, openBlockId: null, latestSuggestions: [], pendingSent }
      }

      const idx = findTurnIdx(state.turns, turnId)
      let nextTurns: Turn[]
      if (idx === -1) {
        // Append new user turn
        nextTurns = [...state.turns, { id: turnId, role: 'user', blocks }]
      } else {
        // Upsert: rebuild the existing turn's blocks (text + any attachments)
        nextTurns = replaceTurn(state.turns, idx, (t) => ({ ...t, blocks }))
      }
      return { ...state, turns: nextTurns, openBlockId: null, latestSuggestions: [], pendingSent }
    }
```

- [ ] **Step 4: Re-assert missing prompts on `replayComplete`**

Replace the `replayComplete` case (~549-553) with:

```ts
    case 'replayComplete': {
      // Re-assert any sent-but-not-yet-on-disk prompts the just-finished replay did
      // not bring back, so a reconnect that raced the transcript-file write does not
      // silently drop the user's prompt. Guarded on text-presence (FIFO) so a prompt
      // already shown — a live echo on a no-reset attach, or one replayed from disk —
      // is never duplicated.
      const present = state.turns.filter((t) => t.role === 'user').map(firstText)
      const toAppend: Turn[] = []
      for (const p of state.pendingSent) {
        const pi = present.indexOf(p.text)
        if (pi === -1) toAppend.push({ id: p.id, role: 'user', blocks: userTurnBlocks(p.id, p.text) })
        else present.splice(pi, 1)  // consume one occurrence (FIFO dup handling)
      }
      return {
        ...state,
        replaying: false,
        turns: toAppend.length ? [...state.turns, ...toAppend] : state.turns,
        earliestTurn: ev.earliestTurn ?? state.earliestTurn,
        totalTurns: ev.totalTurns ?? state.totalTurns,
      }
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run (from `web/`): `npx vitest run src/session/reducer.reassert.test.ts`
Expected: PASS (all tests, including Task 1's).

- [ ] **Step 6: Run the full reducer suite to catch regressions**

Run (from `web/`): `npx vitest run src/session/`
Expected: PASS — existing `reducer.test.ts`, `reducer.optimistic.test.ts`, `history.test.ts` still green (no behavior change for the optimistic-adoption / existing paths).

- [ ] **Step 7: Typecheck + lint**

Run (from `web/`): `npx tsc -b --force && npx eslint src/session/reducer.ts --max-warnings 0`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add web/src/session/reducer.ts web/src/session/reducer.reassert.test.ts
git commit -m "feat(transcript): re-assert sent prompts the replay dropped

Consume the sent-prompt buffer on disk replay; on replayComplete re-append
any buffered prompt not already shown, guarded by FIFO text-presence so it
never duplicates a live echo or a replayed turn.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Dispatch `_trackSentPrompt` when a follow-up is sent

**Files:**
- Modify: `web/src/session/useSessionRuntime.ts` — `onPrompt` (~216-228)

**Interfaces:**
- Consumes: the `_trackSentPrompt` action from Task 1; `dispatch` (already in scope, from `useReducer` at ~85).
- Produces: every normal follow-up prompt is tracked at send time (the armed-question branch is excluded — it echoes its own answer bubble through a separate path).

- [ ] **Step 1: Add the dispatch in the normal-follow-up branch**

In `web/src/session/useSessionRuntime.ts`, change `onPrompt` (~216-228) so the non-armed branch tracks the prompt before sending:

```ts
  const onPrompt = useCallback(
    (text: string, attachmentIds: AttachmentIds) => {
      if (armedQuestionId) {
        answerQuestion(sessionId, armedQuestionId, [text], 'chat')
        dispatch({ type: '_resolveQuestion', questionId: armedQuestionId })
        dispatch({ type: 'user', turnId: `answer:${armedQuestionId}`, text })
        setArmedQuestionId(null)
        return
      }
      // Track the sent prompt so a reconnect whose replayReset outruns the
      // transcript-file write can re-assert it instead of dropping it silently.
      dispatch({ type: '_trackSentPrompt', text })
      prompt(sessionId, text, attachmentIds)
    },
    [sessionId, prompt, answerQuestion, armedQuestionId],
  )
```

- [ ] **Step 2: Typecheck + lint**

Run (from `web/`): `npx tsc -b --force && npx eslint src/session/useSessionRuntime.ts --max-warnings 0`
Expected: no errors. (`dispatch` is already a dependency-stable value from `useReducer`; no dep-array change needed.)

- [ ] **Step 3: Run the full session test suite**

Run (from `web/`): `npx vitest run src/session/`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add web/src/session/useSessionRuntime.ts
git commit -m "feat(session): track sent follow-up prompts for replay re-assert

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Full web verification

**Files:** none (verification only).

- [ ] **Step 1: Full web gate**

Run (from `web/`): `npx tsc -b --force && npx eslint . --max-warnings 0 && npx vitest run`
Expected: all green. If `eslint .` is slow/noisy on unrelated files, scope to changed files: `npx eslint src/session --max-warnings 0`.

- [ ] **Step 2: Manual sanity (optional but recommended)**

In a running instance: open a session, send a follow-up, confirm it shows; force a reconnect (toggle network / background the tab) immediately after sending; confirm the prompt is NOT lost. Navigate away and back; confirm exactly one copy (no duplicate).

- [ ] **Step 3: Finish the branch**

Follow the project completion workflow (CLAUDE.md): push `fix/followup-prompt-reassert`, open a non-draft PR with the Claude Code trailer, then `gh pr merge <#> --squash --auto`. PR body should reference the spec and this plan, and explain the race + the client re-assert fix.

---

## Self-Review

**Spec coverage:**
- Buffer state + `_trackSentPrompt` + cap → Task 1. ✓
- Consume on `replaying` disk replay → Task 2 Step 3. ✓
- Re-assert on `replayComplete` with presence guard (incl. no-wipe attach) → Task 2 Step 4. ✓
- `replayReset` preserves buffer → no code change needed (the case spreads `...state`); covered by a test in Task 2 Step 1. ✓
- Dispatch site in `onPrompt`, armed-question branch excluded → Task 3. ✓
- Scope boundaries (initial prompt untouched, text-only re-assert, queued prompts covered for free, no server change) → respected; no task touches `server/` or the optimistic-seed/adoption logic. ✓
- Tests: track/cap, live-echo-no-consume, disk-replay-consume, race repro, heal, no-wipe guard, FIFO dup, replayReset-preserves → all in Task 2 Step 1 + Task 1 Step 1. ✓

**Placeholder scan:** none — every code/test step shows full content.

**Type consistency:** `pendingSent: { id: string; text: string }[]` and `pendingSentSeq: number` used identically in state, init, and both cases; action `{ type: '_trackSentPrompt'; text: string }` matches its dispatch in Task 3; `firstText(turn: Turn): string` defined in Task 1, used in Task 2; `userTurnBlocks` signature matches existing usage (`userTurnBlocks(turnId, text, images?, ts?)`). ✓
