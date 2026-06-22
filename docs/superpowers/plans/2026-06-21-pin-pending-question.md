# Pin Pending Question to the Composer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface a live, open `AskUserQuestion` in a pinned answer panel above the composer (answer in place, no scrolling); keep historical/replayed questions inline read-only; guarantee answering immediately sends to the agent and clears the affordance.

**Architecture:** A reducer `live` flag (`= !state.replaying`) on each question block distinguishes answerable-now (arrived live over WS) from historical (synthesized during replay). `Turn` suppresses live question blocks inline (they're pinned) and renders historical ones read-only; a new `PendingQuestionPanel` renders the first live-open question above the composer, reusing the existing `QuestionCard`. The "Action needed — scroll up" banner is reduced to permissions only.

**Tech Stack:** React 18 + TypeScript, Vitest + @testing-library/react. Repo uses **pnpm**.

## Global Constraints

- `live` discriminator: a question block's `live === true` iff it arrived while NOT replaying (`!state.replaying`); replayed/historical → `live === false`; absent/undefined → treat as historical (read-only). `state.replaying` starts `true`, flips `false` on the `replayComplete` event.
- **Pin only when answerable:** the pinned panel shows the FIRST question block with `live === true`; nothing on detached/replayed-only sessions.
- **One at a time:** only the first live-open question is pinned; the next pins after the first is answered + removed.
- **Answer → immediate send + cleared (first-class, tested):** answering calls `onAnswer(questionId, selected, mode)` synchronously → `answerQuestion` does an un-batched WS `_send`; `onAnswer` also dispatches `_resolveQuestion` (removes the block) so the panel/banner clear. Multi-question advances to the next without unblocking the turn early.
- Reuse `QuestionCard` for both pinned (interactive) and inline (new `readOnly`) rendering — no parallel question UI.
- Run web tests: `cd web && pnpm vitest run <path>`; full web suite `cd web && pnpm test`; build check `cd web && pnpm build`. Commit only the named files (the working tree has an unrelated `.worktrees/...` deletion — never `git add -A`).

---

### Task 1: Reducer `live` flag on the question block

**Files:**
- Modify: `web/src/session/reducer.ts` (Block `question` type ~line 10; `question` case ~line 287)
- Test: `web/src/session/reducer.test.ts`

**Interfaces:**
- Produces: the `question` Block variant gains `live?: boolean`; set to `!state.replaying` when a `question` event is reduced.

- [ ] **Step 1: Write the failing test**

Append to `web/src/session/reducer.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

describe('question live flag', () => {
  const qEvent = { type: 'question', questionId: 'q1', turnId: 't1', header: 'H', question: 'Q?', options: [{ label: 'A' }], multiSelect: false } as const

  it('marks a question received during replay as not live', () => {
    // initialTranscript.replaying === true
    const s = transcriptReducer(initialTranscript, qEvent)
    const block = s.turns.flatMap((t) => t.blocks).find((b) => b.kind === 'question')
    expect(block && 'live' in block && block.live).toBe(false)
  })

  it('marks a question received after replayComplete as live', () => {
    const afterReplay = transcriptReducer(initialTranscript, { type: 'replayComplete' } as const)
    const s = transcriptReducer(afterReplay, qEvent)
    const block = s.turns.flatMap((t) => t.blocks).find((b) => b.kind === 'question')
    expect(block && 'live' in block && block.live).toBe(true)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && pnpm vitest run src/session/reducer.test.ts -t "question live flag"`
Expected: FAIL — `block.live` is `undefined`.

- [ ] **Step 3: Implement**

In `web/src/session/reducer.ts`, add `live` to the `question` Block variant:

```ts
  | { kind: 'question';   id: string; questionId: string; header: string; question: string; options: Array<Record<string, unknown>>; multiSelect: boolean; live?: boolean }
```

In the `case 'question':` block, set `live` on the new block:

```ts
      const newBlock: Block = { kind: 'question', id: questionId, questionId, header, question, options, multiSelect, live: !state.replaying }
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd web && pnpm vitest run src/session/reducer.test.ts`
Expected: PASS (new + existing reducer tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/session/reducer.ts web/src/session/reducer.test.ts
git commit -m "feat(session): mark question blocks live vs replayed (=!replaying)"
```

---

### Task 2: `QuestionCard` read-only mode

**Files:**
- Modify: `web/src/components/session/QuestionCard.tsx`
- Test: `web/src/components/session/__tests__/QuestionCard.test.tsx`

**Interfaces:**
- Produces: `QuestionCard` gains `readOnly?: boolean`. When `true`, renders the header + question + options inertly (no `onAnswer`/`onChat` wiring, no submit, no escape hatches).

- [ ] **Step 1: Write the failing test**

Append to `web/src/components/session/__tests__/QuestionCard.test.tsx`:

```tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { QuestionCard } from '../QuestionCard'

const block = {
  kind: 'question' as const, id: 'q1', questionId: 'q1', header: 'Layout',
  question: 'Two-pane?', options: [{ label: 'Yes' }, { label: 'No' }], multiSelect: false,
}

describe('QuestionCard readOnly', () => {
  it('renders options inertly and never calls onAnswer when readOnly', () => {
    const onAnswer = vi.fn()
    render(<QuestionCard block={block} onAnswer={onAnswer} readOnly />)
    expect(screen.getByText('Two-pane?')).toBeInTheDocument()
    expect(screen.getByText('Yes')).toBeInTheDocument()
    // No interactive option buttons (read-only): clicking the label does nothing.
    fireEvent.click(screen.getByText('Yes'))
    expect(onAnswer).not.toHaveBeenCalled()
    // No write/chat escape hatches in read-only mode.
    expect(screen.queryByText(/Write a response/)).toBeNull()
    expect(screen.queryByText(/Chat about this/)).toBeNull()
  })

  it('interactive mode still answers on option click', () => {
    const onAnswer = vi.fn()
    render(<QuestionCard block={block} onAnswer={onAnswer} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Yes' }))
    expect(onAnswer).toHaveBeenCalledWith('q1', ['Yes'])
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && pnpm vitest run src/components/session/__tests__/QuestionCard.test.tsx -t readOnly`
Expected: FAIL — readOnly prop unknown; clicking "Yes" still calls `onAnswer` (interactive button) or the inert markup isn't present.

- [ ] **Step 3: Implement**

In `web/src/components/session/QuestionCard.tsx`, add `readOnly` to `Props`:

```tsx
interface Props {
  block: QuestionBlock
  onAnswer: (questionId: string, selected: string[], mode?: AnswerMode) => void
  onChat?: (questionId: string) => void
  armedForChat?: boolean
  readOnly?: boolean
}
```

Update the signature and add an early read-only return right after `heading` is defined (before the `armedForChat` branch):

```tsx
export function QuestionCard({ block, onAnswer, onChat, armedForChat = false, readOnly = false }: Props) {
```

```tsx
  // Read-only (historical / replayed) — show what was asked, no interaction.
  if (readOnly) {
    return (
      <div role="group" aria-labelledby={headerId} className={containerClass}>
        {heading}
        <ul className="space-y-1">
          {block.options.map((opt, i) => (
            <li
              key={i}
              className={cn(
                'rounded px-3 py-2 text-sm',
                'bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700',
                'text-gray-500 dark:text-gray-400',
              )}
            >
              {optionLabel(opt)}
            </li>
          ))}
        </ul>
      </div>
    )
  }
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd web && pnpm vitest run src/components/session/__tests__/QuestionCard.test.tsx`
Expected: PASS (read-only inert + interactive still answers + any existing QuestionCard tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/QuestionCard.tsx web/src/components/session/__tests__/QuestionCard.test.tsx
git commit -m "feat(session): QuestionCard read-only mode for historical questions"
```

---

### Task 3: `Turn` — suppress live questions inline, render historical read-only

**Files:**
- Modify: `web/src/components/session/Turn.tsx` (`case 'question'` ~line 67)
- Test: `web/src/components/session/__tests__/Turn.test.tsx` (create if absent)

**Interfaces:**
- Consumes: question block `live` (Task 1), `QuestionCard` `readOnly` (Task 2).
- Produces: inline rendering shows historical (`!live`) questions read-only and omits live ones (they're pinned by Task 4).

- [ ] **Step 1: Write the failing test**

Create/append `web/src/components/session/__tests__/Turn.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { Turn } from '../Turn'

function turnWith(block: any) {
  return { role: 'assistant' as const, srcUuid: 't1', blocks: [block] }
}
const baseQ = { kind: 'question' as const, id: 'q1', questionId: 'q1', header: 'Layout', question: 'Two-pane?', options: [{ label: 'Yes' }], multiSelect: false }

const noop = () => {}

describe('Turn question rendering', () => {
  it('does not render a LIVE question inline (it is pinned elsewhere)', () => {
    render(<Turn turn={turnWith({ ...baseQ, live: true })} isLastTurn permissionMode="interactive"
      toolDetailMode="auto" onDecision={noop} onAnswer={noop} onChat={noop} armedQuestionId={null} />)
    expect(screen.queryByText('Two-pane?')).toBeNull()
  })

  it('renders a historical (non-live) question inline, read-only', () => {
    const onAnswer = vi.fn()
    render(<Turn turn={turnWith({ ...baseQ, live: false })} isLastTurn permissionMode="interactive"
      toolDetailMode="auto" onDecision={noop} onAnswer={onAnswer} onChat={noop} armedQuestionId={null} />)
    expect(screen.getByText('Two-pane?')).toBeInTheDocument()
    // Read-only: no interactive radio that would call onAnswer.
    expect(screen.queryByRole('radio')).toBeNull()
  })
})
```

(Match `Turn`'s actual prop names — read the top of `Turn.tsx`; adapt `turn`/`isLastTurn`/`streamingBlockId` etc. as needed.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && pnpm vitest run src/components/session/__tests__/Turn.test.tsx`
Expected: FAIL — the live question currently renders inline (interactive), so "Two-pane?" is present and a radio exists.

- [ ] **Step 3: Implement**

In `web/src/components/session/Turn.tsx`, replace the `case 'question':` block:

```tsx
          case 'question':
            // A LIVE open question is rendered by the pinned PendingQuestionPanel
            // above the composer — omit it inline to avoid a duplicate. Historical
            // (replayed) questions render inline, read-only.
            if (block.live) return null
            return <QuestionCard key={block.id} block={block} onAnswer={onAnswer} readOnly />
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd web && pnpm vitest run src/components/session/__tests__/Turn.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/Turn.tsx web/src/components/session/__tests__/Turn.test.tsx
git commit -m "feat(session): inline questions read-only; live ones pinned, not duplicated"
```

---

### Task 4: `PendingQuestionPanel` + wire into `SessionView` (answer → send + clear)

**Files:**
- Create: `web/src/components/session/PendingQuestionPanel.tsx`
- Modify: `web/src/components/session/SessionView.tsx` (between `<SessionBody>` and the Composer `<div>`)
- Test: `web/src/components/session/__tests__/PendingQuestionPanel.test.tsx`, `web/src/session/useSessionRuntime.test.ts` (answer-flow guard; create if absent)

**Interfaces:**
- Consumes: question block `live` (Task 1), `QuestionCard` interactive (existing), `rt.state.turns`/`rt.onAnswer`/`rt.onChat`/`rt.armedQuestionId`.
- Produces: `findFirstLiveQuestion(turns) -> QuestionBlock | null`; `<PendingQuestionPanel turns onAnswer onChat armedQuestionId />`.

- [ ] **Step 1: Write the failing test (panel + answer flow)**

Create `web/src/components/session/__tests__/PendingQuestionPanel.test.tsx`:

```tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { PendingQuestionPanel } from '../PendingQuestionPanel'

const q = (id: string, live: boolean) => ({
  kind: 'question' as const, id, questionId: id, header: id, question: `${id}?`,
  options: [{ label: 'Yes' }], multiSelect: false, live,
})
const turn = (...blocks: any[]) => ({ role: 'assistant' as const, srcUuid: 't', blocks })

describe('PendingQuestionPanel', () => {
  it('renders nothing when there is no live question (detached/replay)', () => {
    const { container } = render(
      <PendingQuestionPanel turns={[turn(q('a', false))]} onAnswer={vi.fn()} onChat={vi.fn()} armedQuestionId={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('pins the first LIVE open question and answers it synchronously on click', () => {
    const onAnswer = vi.fn()
    render(<PendingQuestionPanel turns={[turn(q('a', true))]} onAnswer={onAnswer} onChat={vi.fn()} armedQuestionId={null} />)
    expect(screen.getByText('a?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Yes' }))
    expect(onAnswer).toHaveBeenCalledWith('a', ['Yes'])   // immediate, synchronous send
  })

  it('one at a time: shows only the first live question; the second after the first is gone', () => {
    const { rerender } = render(
      <PendingQuestionPanel turns={[turn(q('a', true), q('b', true))]} onAnswer={vi.fn()} onChat={vi.fn()} armedQuestionId={null} />)
    expect(screen.getByText('a?')).toBeInTheDocument()
    expect(screen.queryByText('b?')).toBeNull()
    // After 'a' is answered/removed, 'b' pins.
    rerender(<PendingQuestionPanel turns={[turn(q('b', true))]} onAnswer={vi.fn()} onChat={vi.fn()} armedQuestionId={null} />)
    expect(screen.getByText('b?')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && pnpm vitest run src/components/session/__tests__/PendingQuestionPanel.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `PendingQuestionPanel`**

Create `web/src/components/session/PendingQuestionPanel.tsx`:

```tsx
import type { Turn, Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { QuestionCard } from './QuestionCard'

type QuestionBlock = Extract<Block, { kind: 'question' }>

interface Props {
  turns: Turn[]
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  onChat: (questionId: string) => void
  armedQuestionId: string | null
}

/** First live, open question across all turns (one-at-a-time pinning). */
export function findFirstLiveQuestion(turns: Turn[]): QuestionBlock | null {
  for (const t of turns) {
    for (const b of t.blocks) {
      if (b.kind === 'question' && b.live) return b
    }
  }
  return null
}

export function PendingQuestionPanel({ turns, onAnswer, onChat, armedQuestionId }: Props) {
  const q = findFirstLiveQuestion(turns)
  if (!q) return null
  return (
    <div
      className={cn(
        'flex-shrink-0 border-t border-amber-200 dark:border-amber-800',
        'bg-amber-50/60 dark:bg-amber-950/20 px-3 py-2',
      )}
      data-testid="pending-question-panel"
    >
      <QuestionCard
        block={q}
        onAnswer={onAnswer}
        onChat={onChat}
        armedForChat={q.questionId === armedQuestionId}
      />
    </div>
  )
}
```

- [ ] **Step 4: Wire into `SessionView`**

In `web/src/components/session/SessionView.tsx`, import and render the panel between `<SessionBody rt={rt} />` and the Composer `<div>`:

```tsx
import { PendingQuestionPanel } from './PendingQuestionPanel'
```

```tsx
      {/* Transcript + status strip + banners */}
      <SessionBody rt={rt} />

      {/* Pinned answer panel for the live open question (answer in place) */}
      <PendingQuestionPanel
        turns={rt.state.turns}
        onAnswer={rt.onAnswer}
        onChat={rt.onChat}
        armedQuestionId={rt.armedQuestionId}
      />

      {/* Composer */}
      <div className="flex-shrink-0">
        <Composer
```

- [ ] **Step 5: Write the runtime answer-flow guard test (immediate send + clear)**

Create/append `web/src/session/useSessionRuntime.test.ts` — verify `onAnswer` sends immediately AND resolves the block. Mock the WS layer the runtime uses (`answerQuestion`). Read the top of `useSessionRuntime.ts` for the exact import/hook to mock (it consumes `answerQuestion` from the websocket hook/module); then:

```ts
import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'

// Mock the websocket module so we can assert the send fires synchronously.
const answerQuestion = vi.fn()
vi.mock('@/ws/client', async (orig) => {
  const actual = await orig<typeof import('@/ws/client')>()
  return { ...actual, /* adapt: expose a hook/object whose answerQuestion is the mock */ }
})

import { useSessionRuntime } from './useSessionRuntime'

describe('useSessionRuntime.onAnswer', () => {
  it('sends the answer immediately and resolves the question block', () => {
    const { result } = renderHook(() => useSessionRuntime('sess1'))
    // Seed a live question into state via the runtime's dispatch path, then answer it.
    act(() => { result.current.onAnswer('q1', ['Yes']) })
    expect(answerQuestion).toHaveBeenCalledWith('sess1', 'q1', ['Yes'], 'option')
    // The question block is resolved (removed) — no longer open.
    expect(result.current.state.turns.flatMap((t) => t.blocks).some((b) => b.kind === 'question' && b.questionId === 'q1')).toBe(false)
  })
})
```

If mocking the websocket hook is impractical in isolation, instead assert via the existing runtime test harness (search `useSessionRuntime` usages in tests) — the binding requirement is: **`onAnswer` calls `answerQuestion` synchronously and dispatches `_resolveQuestion`** (both already in `useSessionRuntime.onAnswer`). Adapt the mock to the real module boundary; do not weaken to a tautology.

- [ ] **Step 6: Run + full web suite**

Run: `cd web && pnpm vitest run src/components/session/__tests__/PendingQuestionPanel.test.tsx src/session/useSessionRuntime.test.ts`
Expected: PASS.
Run: `cd web && pnpm test` — full suite green.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/session/PendingQuestionPanel.tsx web/src/components/session/SessionView.tsx web/src/components/session/__tests__/PendingQuestionPanel.test.tsx web/src/session/useSessionRuntime.test.ts
git commit -m "feat(session): pin live question above composer; answer in place (immediate send + clear)"
```

---

### Task 5: Reduce the "Action needed" banner to permissions only

**Files:**
- Modify: `web/src/components/session/SessionBody.tsx` (the `rt.hasOpenAction` banner ~line 78)
- Test: `web/src/components/session/__tests__/SessionView.test.tsx` (or a SessionBody test)

**Interfaces:**
- Consumes: `rt.state.turns` (permission blocks).
- Produces: the banner renders only when an open **permission** exists; an open question alone shows no banner (it's pinned).

- [ ] **Step 1: Write the failing test**

Add to the relevant SessionBody/SessionView test (read the existing harness for how `rt`/state is built):

```tsx
// With an open QUESTION only (no permission), the "Action needed — scroll up" banner must NOT render.
// With an open PERMISSION, it MUST render.
```

Concretely, render `SessionBody` with a state whose turns contain only a `question` block → assert `screen.queryByText(/Action needed/)` is null; with a `permission` block → assert it is present. (Build the `rt` stub the way the existing SessionBody/SessionView tests do.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && pnpm vitest run src/components/session/__tests__/SessionView.test.tsx`
Expected: FAIL — the banner currently shows for an open question (via `hasOpenAction`).

- [ ] **Step 3: Implement**

In `web/src/components/session/SessionBody.tsx`, compute a permission-only flag and gate the banner on it:

```tsx
  const hasOpenPermission = rt.state.turns.some((t) => t.blocks.some((b) => b.kind === 'permission'))
```

Change the banner condition from `{rt.hasOpenAction && (` to `{hasOpenPermission && (` and keep the "Action needed — scroll up to respond" text (permissions still render inline). Leave the rest of `SessionBody` unchanged.

- [ ] **Step 4: Run to verify it passes**

Run: `cd web && pnpm vitest run src/components/session/__tests__/SessionView.test.tsx`
Expected: PASS — banner only for permissions.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/SessionBody.tsx web/src/components/session/__tests__/SessionView.test.tsx
git commit -m "feat(session): action-needed banner only for open permissions (questions are pinned)"
```

---

### Task 6: Full web suite + build + typecheck

**Files:** none (verification)

- [ ] **Step 1: Full web suite**

Run: `cd web && pnpm test`
Expected: all green (including existing SessionView/QuestionCard/reducer tests adjusted for the new behavior).

- [ ] **Step 2: Typecheck + build (the deploy gate runs `tsc -b && vite build`)**

Run: `cd web && pnpm exec tsc --noEmit` → clean.
Run: `cd web && pnpm build` → succeeds.

- [ ] **Step 3: Commit (only if a typecheck fixup was needed)**

```bash
git add web/src && git commit -m "chore(web): typecheck fixups for pinned question panel" || echo "nothing to fix"
```

---

## Notes for the implementer

- `QuestionCard` is the single source of truth for question rendering — pinned uses it interactively, inline uses it `readOnly`. Do not fork a second question component.
- Commit only the named files. The working tree carries an unrelated `.worktrees/...` deletion from a parallel session — `git add <files>`, never `-A`.
- The "one at a time" behavior needs no explicit state: `findFirstLiveQuestion` returns the first live-open block; answering removes it (`_resolveQuestion`), so the next becomes first on the next render.
- Don't touch the freeform/chat escape hatches — they're reused as-is in the pinned (interactive) `QuestionCard`.
