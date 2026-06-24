# Grouped Multi-Question Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render per-option descriptions, batch a multi-question `AskUserQuestion` call behind a single submit with freely-revisitable answers, and stop freeform replies leaking between questions.

**Architecture:** All changes are in the React layer under `web/src/components/session/`. `QuestionCard` gains (a) description rendering and (b) an optional *controlled* mode (`staged` + `onStage`) that stages an answer instead of sending it. A new `QuestionGroup` holds the staged answers for 2+ questions and fires the existing per-question `onAnswer` once each on submit. `PendingQuestionPanel` renders a single immediate-send `QuestionCard` for one live question, or a `QuestionGroup` for several. No backend, protocol, or wire-format changes — the driver already forwards `description` and already `asyncio.gather`s all answer futures.

**Tech Stack:** React + TypeScript + Vite, Tailwind, Vitest + @testing-library/react + @testing-library/user-event.

## Global Constraints

- Web tests run from `web/`: `npx vitest run`, `npx tsc -b --force` (the real type gate — `tsc --noEmit` checks nothing here), `npx eslint .` with `--max-warnings 0`.
- Use the local `node_modules/.bin` tools, not `npx`-fetched ones; if `node_modules` is missing in this worktree, symlink it from the deploy worktree.
- Single-question behavior MUST stay immediate-send and unchanged (no new Submit step for one question).
- Grouped (2+) submit MUST stay disabled until every question has a non-empty answer, then fire exactly one `onAnswer(questionId, selected, mode)` per question.
- `onAnswer` signature is `(questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void` — do not change it.
- Match existing Tailwind token usage and the 44px min touch target already used in `QuestionCard`.

---

### Task 1: Render option descriptions

**Files:**
- Modify: `web/src/components/session/QuestionCard.tsx`
- Test: `web/src/components/session/__tests__/QuestionCard.test.tsx`

**Interfaces:**
- Consumes: `QuestionBlock` from `@/session/reducer` (`options: Array<Record<string, unknown>>`; each option may carry a string `description`).
- Produces: `optionDescription(opt: Record<string, unknown>): string | null` (module-local helper in `QuestionCard.tsx`).

- [ ] **Step 1: Write the failing tests**

Add to `QuestionCard.test.tsx`. First add a fixture near the top (after `multiQ`):

```tsx
const describedQ: QuestionBlock = {
  ...singleQ,
  id: 'q-desc',
  questionId: 'q-desc',
  options: [
    { label: 'Option A', description: 'The first choice' },
    { label: 'Option B' },
  ],
}
```

Then add these tests inside `describe('QuestionCard', ...)`:

```tsx
it('renders an option description when present', () => {
  render(<QuestionCard block={describedQ} onAnswer={vi.fn()} />)
  expect(screen.getByText('The first choice')).toBeInTheDocument()
})

it('renders no description element for options without one', () => {
  render(<QuestionCard block={describedQ} onAnswer={vi.fn()} />)
  // Option B has no description; only one description string exists.
  expect(screen.queryAllByText(/choice/i)).toHaveLength(1)
})

it('renders descriptions in read-only mode', () => {
  render(<QuestionCard block={describedQ} onAnswer={vi.fn()} readOnly />)
  expect(screen.getByText('The first choice')).toBeInTheDocument()
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx -t "description"`
Expected: FAIL — "The first choice" not found (descriptions not rendered yet).

- [ ] **Step 3: Add the helper and render descriptions**

In `QuestionCard.tsx`, add below `optionLabel` (line ~20):

```tsx
function optionDescription(opt: Record<string, unknown>): string | null {
  const d = opt.description
  return typeof d === 'string' && d.trim() ? d : null
}
```

In `SingleSelectOptions`, replace the option button body (currently `{label}` at line ~120) with:

```tsx
<span className="block">{label}</span>
{optionDescription(opt) && (
  <span className="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
    {optionDescription(opt)}
  </span>
)}
```

In `MultiSelectOptions`, replace `<span>{label}</span>` (line ~70) with a stacked label + description. Change the row's flex alignment from `items-center` to `items-start` so the checkbox aligns to the first line:

```tsx
<span aria-hidden="true" className="inline-block w-4">{isChecked ? '☑' : '☐'}</span>
<span className="block">
  <span className="block">{label}</span>
  {optionDescription(opt) && (
    <span className="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
      {optionDescription(opt)}
    </span>
  )}
</span>
```

In the `readOnly` branch's `<li>` (line ~150-160), replace `{optionLabel(opt)}` with:

```tsx
<span className="block">{optionLabel(opt)}</span>
{optionDescription(opt) && (
  <span className="block text-xs text-gray-400 dark:text-gray-500 mt-0.5">
    {optionDescription(opt)}
  </span>
)}
```

Also: the `readOnly` `<li>` currently uses `key={optionLabel(opt)}` — leave as is (labels are unique within a question).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx`
Expected: PASS (all existing + 3 new). Existing option-name lookups like `getByRole('radio', { name: 'Option A' })` still pass because `singleQ`/`multiQ` options have no description.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/QuestionCard.tsx web/src/components/session/__tests__/QuestionCard.test.tsx
git commit -m "feat(web): render AskUserQuestion option descriptions

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Fix freeform-reply carryover between questions

**Files:**
- Modify: `web/src/components/session/PendingQuestionPanel.tsx`
- Test: `web/src/components/session/__tests__/PendingQuestionPanel.test.tsx`

**Interfaces:**
- Consumes: existing `PendingQuestionPanel` props (unchanged in this task).
- Produces: a `key={q.questionId}` on the rendered `QuestionCard` so React remounts per question and never reuses `draft`/`writing` state.

- [ ] **Step 1: Write the failing test**

Add to `PendingQuestionPanel.test.tsx` inside `describe('PendingQuestionPanel', ...)`:

```tsx
it('does not carry a freeform draft from one question to the next', async () => {
  const user = userEvent.setup()
  const { rerender } = render(
    <PendingQuestionPanel turns={[turn(q('a', true))]} onAnswer={vi.fn()} onChat={vi.fn()} armedQuestionId={null} />)
  await user.click(screen.getByRole('button', { name: /write a response/i }))
  await user.type(screen.getByRole('textbox', { name: /write a response/i }), 'leaky draft')
  // Question 'a' is answered/removed; 'b' takes its place at the same tree slot.
  rerender(
    <PendingQuestionPanel turns={[turn(q('b', true))]} onAnswer={vi.fn()} onChat={vi.fn()} armedQuestionId={null} />)
  // The new question shows its options fresh — no textarea, no leaked text.
  expect(screen.queryByRole('textbox', { name: /write a response/i })).toBeNull()
  expect(screen.queryByText('leaky draft')).toBeNull()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/PendingQuestionPanel.test.tsx -t "carry a freeform draft"`
Expected: FAIL — the textarea with "leaky draft" survives the rerender (same `QuestionCard` instance, no `key`).

- [ ] **Step 3: Add the key**

In `PendingQuestionPanel.tsx`, change the rendered card (line ~38) from:

```tsx
<QuestionCard
  block={q}
  onAnswer={onAnswer}
  onChat={onChat}
  armedForChat={q.questionId === armedQuestionId}
/>
```

to add `key={q.questionId}`:

```tsx
<QuestionCard
  key={q.questionId}
  block={q}
  onAnswer={onAnswer}
  onChat={onChat}
  armedForChat={q.questionId === armedQuestionId}
/>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/PendingQuestionPanel.test.tsx`
Expected: PASS (all existing + new).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/PendingQuestionPanel.tsx web/src/components/session/__tests__/PendingQuestionPanel.test.tsx
git commit -m "fix(web): remount QuestionCard per question so freeform drafts don't leak

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Add controlled (staged) mode to QuestionCard — single-select + freeform

**Files:**
- Modify: `web/src/components/session/QuestionCard.tsx`
- Test: `web/src/components/session/__tests__/QuestionCard.test.tsx`

**Interfaces:**
- Consumes: `AnswerMode = 'option' | 'freeform' | 'chat'` (already defined in `QuestionCard.tsx`).
- Produces: extended `Props` on `QuestionCard`:
  - `staged?: StagedAnswer | null` — the currently-staged answer (controlled mode marker when `onStage` is also set).
  - `onStage?: (questionId: string, answer: StagedAnswer) => void` — when present, the card stages instead of sending; it never calls `onAnswer` and hides the chat hatch.
  - `type StagedAnswer = { selected: string[]; mode: Exclude<AnswerMode, 'chat'> }` (export it).
- Behavior contract for grouped/controlled single-select:
  - clicking or keyboard-activating an option calls `onStage(questionId, { selected: [label], mode: 'option' })`.
  - the staged option is visually highlighted and `aria-checked={true}`.
  - "Write a response" + typing + "Set response" calls `onStage(questionId, { selected: [text], mode: 'freeform' })`; an empty draft does nothing.
  - the "Chat about this" hatch is not rendered.

- [ ] **Step 1: Write the failing tests**

Add to `QuestionCard.test.tsx` a new describe block at the end of the file:

```tsx
describe('QuestionCard grouped (staged) single-select', () => {
  it('staging an option calls onStage instead of onAnswer', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    const onStage = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} onStage={onStage} staged={null} />)
    await user.click(screen.getByRole('radio', { name: 'Option A' }))
    expect(onStage).toHaveBeenCalledWith('q-1', { selected: ['Option A'], mode: 'option' })
    expect(onAnswer).not.toHaveBeenCalled()
  })

  it('reflects the staged option as checked', () => {
    render(
      <QuestionCard
        block={singleQ}
        onAnswer={vi.fn()}
        onStage={vi.fn()}
        staged={{ selected: ['Option B'], mode: 'option' }}
      />)
    expect(screen.getByRole('radio', { name: 'Option B' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'Option A' })).toHaveAttribute('aria-checked', 'false')
  })

  it('hides the chat hatch in grouped mode but keeps write-a-response', () => {
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} onStage={vi.fn()} onChat={vi.fn()} staged={null} />)
    expect(screen.queryByRole('button', { name: /chat about this/i })).toBeNull()
    expect(screen.getByRole('button', { name: /write a response/i })).toBeInTheDocument()
  })

  it('grouped freeform stages text instead of sending', async () => {
    const user = userEvent.setup()
    const onStage = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} onStage={onStage} staged={null} />)
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    await user.type(screen.getByRole('textbox', { name: /write a response/i }), 'custom text')
    await user.click(screen.getByRole('button', { name: /set response/i }))
    expect(onStage).toHaveBeenCalledWith('q-1', { selected: ['custom text'], mode: 'freeform' })
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx -t "grouped \\(staged\\) single-select"`
Expected: FAIL — `onStage` prop unknown / chat hatch still present / no "Set response" button.

- [ ] **Step 3: Implement controlled single-select + freeform**

In `QuestionCard.tsx`:

(a) Export the staged-answer type near the top (after `type AnswerMode`):

```tsx
export type StagedAnswer = { selected: string[]; mode: Exclude<AnswerMode, 'chat'> }
```

(b) Extend `Props`:

```tsx
interface Props {
  block: QuestionBlock
  onAnswer: (questionId: string, selected: string[], mode?: AnswerMode) => void
  onChat?: (questionId: string) => void
  armedForChat?: boolean
  readOnly?: boolean
  staged?: StagedAnswer | null
  onStage?: (questionId: string, answer: StagedAnswer) => void
}
```

(c) Change `SingleSelectOptions` to take a generic pick callback + optional selected highlight. Replace its signature and body's activation wiring:

```tsx
function SingleSelectOptions({
  block, onPick, selectedLabel,
}: { block: QuestionBlock; onPick: (label: string) => void; selectedLabel?: string }) {
  const headerId = `q-header-${block.questionId}`
  const kb = useCardKeyboard({
    count: block.options.length,
    onActivate: (i) => onPick(optionLabel(block.options[i])),
  })
  return (
    <div role="radiogroup" aria-labelledby={headerId} className="space-y-1" {...kb.containerProps}>
      {block.options.map((opt, i) => {
        const label = optionLabel(opt)
        const checked = selectedLabel !== undefined ? label === selectedLabel : i === kb.activeIndex
        return (
          <button
            key={i}
            type="button"
            role="radio"
            aria-checked={checked}
            {...kb.itemProps(i)}
            onClick={() => onPick(label)}
            className={cn(
              'w-full text-left rounded px-3 py-2 text-sm',
              'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
              'text-gray-800 dark:text-gray-200',
              'hover:bg-blue-50 dark:hover:bg-blue-900/30',
              checked && selectedLabel !== undefined && 'ring-2 ring-blue-400 bg-blue-50 dark:bg-blue-900/30',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              'min-h-[44px]',
            )}
          >
            <span className="block">{label}</span>
            {optionDescription(opt) && (
              <span className="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                {optionDescription(opt)}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
```

(d) In the main `QuestionCard` body, compute grouped mode and wire the escape hatch + single-select. Add near the top of the component (after the `useState`s):

```tsx
const grouped = typeof onStage === 'function'
```

Replace the freeform "Send response" button's `onClick` so it stages when grouped, and relabel it. In `escapeHatches`, change the button to:

```tsx
<button
  type="button"
  onClick={() => {
    const t = draft.trim()
    if (!t) return
    if (grouped) onStage!(block.questionId, { selected: [t], mode: 'freeform' })
    else onAnswer(block.questionId, [t], 'freeform')
  }}
  className={cn(
    'rounded px-4 py-2 text-sm font-medium min-h-[44px]',
    'bg-blue-600 text-white hover:bg-blue-700',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
  )}
>
  {grouped ? 'Set response' : 'Send response'}
</button>
```

And hide the chat hatch when grouped — change the non-writing branch:

```tsx
) : (
  <div className="flex flex-wrap gap-2">
    <button type="button" onClick={() => setWriting(true)} className={hatchBtn}>
      ✍️ Write a response
    </button>
    {!grouped && (
      <button type="button" onClick={() => onChat?.(block.questionId)} className={hatchBtn}>
        💬 Chat about this
      </button>
    )}
  </div>
)}
```

Replace the single-select render branch (the `if (!block.multiSelect)` block) to pass the pick callback + staged highlight:

```tsx
if (!block.multiSelect) {
  return (
    <div role="group" aria-labelledby={headerId} className={containerClass}>
      {heading}
      <SingleSelectOptions
        block={block}
        onPick={(label) =>
          grouped
            ? onStage!(block.questionId, { selected: [label], mode: 'option' })
            : onAnswer(block.questionId, [label])
        }
        selectedLabel={grouped ? (staged?.selected[0] ?? '') : undefined}
      />
      {escapeHatches}
    </div>
  )
}
```

Note: when grouped and nothing staged, `selectedLabel` is `''` (a defined empty string), so no option highlights and keyboard `activeIndex` highlight is suppressed — acceptable; staging on click/Enter still works via `onPick`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx`
Expected: PASS — new grouped single-select tests pass; all prior tests still pass (uncontrolled path: `onStage` undefined → `grouped` false → unchanged behavior, including keyboard-`activeIndex` highlight since `selectedLabel` is `undefined`).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/QuestionCard.tsx web/src/components/session/__tests__/QuestionCard.test.tsx
git commit -m "feat(web): add staged (controlled) single-select + freeform mode to QuestionCard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Controlled (staged) multi-select in QuestionCard

**Files:**
- Modify: `web/src/components/session/QuestionCard.tsx`
- Test: `web/src/components/session/__tests__/QuestionCard.test.tsx`

**Interfaces:**
- Consumes: `StagedAnswer`, `onStage`, `staged` from Task 3.
- Produces: grouped multi-select behavior:
  - the checked set is driven by `staged.selected` (controlled); toggling an option calls `onStage(questionId, { selected: nextLabels, mode: 'option' })`.
  - no per-card "Submit" button is shown in grouped mode (the group owns submit).
  - uncontrolled mode keeps its internal `checked` state and its own "Submit" button exactly as today.

- [ ] **Step 1: Write the failing tests**

Add a describe block to `QuestionCard.test.tsx`:

```tsx
describe('QuestionCard grouped (staged) multi-select', () => {
  it('toggling stages the next checked set and shows no per-card submit', async () => {
    const user = userEvent.setup()
    const onStage = vi.fn()
    render(<QuestionCard block={multiQ} onAnswer={vi.fn()} onStage={onStage} staged={{ selected: [], mode: 'option' }} />)
    expect(screen.queryByRole('button', { name: /^submit$/i })).toBeNull()
    await user.click(screen.getByRole('checkbox', { name: /option a/i }))
    expect(onStage).toHaveBeenCalledWith('q-2', { selected: ['Option A'], mode: 'option' })
  })

  it('reflects the staged checked set', () => {
    render(<QuestionCard block={multiQ} onAnswer={vi.fn()} onStage={vi.fn()} staged={{ selected: ['Option B'], mode: 'option' }} />)
    expect(screen.getByRole('checkbox', { name: /option b/i })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('checkbox', { name: /option a/i })).toHaveAttribute('aria-checked', 'false')
  })

  it('unchecks a staged option on toggle', async () => {
    const user = userEvent.setup()
    const onStage = vi.fn()
    render(<QuestionCard block={multiQ} onAnswer={vi.fn()} onStage={onStage} staged={{ selected: ['Option A'], mode: 'option' }} />)
    await user.click(screen.getByRole('checkbox', { name: /option a/i }))
    expect(onStage).toHaveBeenCalledWith('q-2', { selected: [], mode: 'option' })
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx -t "grouped \\(staged\\) multi-select"`
Expected: FAIL — grouped multi-select still shows the Submit button and uses internal state.

- [ ] **Step 3: Make MultiSelectOptions controllable**

In `QuestionCard.tsx`, rewrite `MultiSelectOptions` so the checked set and toggle handler can be supplied by the parent; keep an uncontrolled fallback. Replace the whole function:

```tsx
function MultiSelectOptions({
  block, checked, onToggle, onSubmit, showSubmit,
}: {
  block: QuestionBlock
  checked: Set<string>
  onToggle: (label: string) => void
  onSubmit?: () => void
  showSubmit: boolean
}) {
  const kb = useCardKeyboard({
    count: block.options.length,
    multiSelect: true,
    onToggle: (i) => onToggle(optionLabel(block.options[i])),
    onSubmit: () => onSubmit?.(),
  })
  return (
    <>
      <div className="space-y-1 mb-3" {...kb.containerProps}>
        {block.options.map((opt, i) => {
          const label = optionLabel(opt)
          const isChecked = checked.has(label)
          return (
            <button
              key={i}
              type="button"
              role="checkbox"
              aria-checked={isChecked}
              {...kb.itemProps(i)}
              onClick={() => onToggle(label)}
              className={cn(
                'flex items-start gap-2 w-full text-left rounded px-3 py-2 text-sm',
                'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
                'text-gray-800 dark:text-gray-200',
                'hover:bg-blue-50 dark:hover:bg-blue-900/30',
                isChecked && 'ring-2 ring-blue-400 bg-blue-50 dark:bg-blue-900/30',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                'min-h-[44px]',
              )}
            >
              <span aria-hidden="true" className="inline-block w-4">{isChecked ? '☑' : '☐'}</span>
              <span className="block">
                <span className="block">{label}</span>
                {optionDescription(opt) && (
                  <span className="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                    {optionDescription(opt)}
                  </span>
                )}
              </span>
            </button>
          )
        })}
      </div>
      {showSubmit && (
        <button
          type="button"
          onClick={() => onSubmit?.()}
          className={cn(
            'rounded px-4 py-2 text-sm font-medium',
            'bg-blue-600 text-white hover:bg-blue-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            'min-h-[44px]',
          )}
        >
          Submit
        </button>
      )}
    </>
  )
}
```

(b) The uncontrolled checked state now lives in `QuestionCard`. Add to the component body (with the other `useState`s):

```tsx
const [localChecked, setLocalChecked] = useState<Set<string>>(new Set())
```

(c) Replace the multi-select render branch (the final `return` of `QuestionCard`) with grouped-vs-uncontrolled wiring:

```tsx
const stagedSet = new Set(staged?.selected ?? [])
const toggleStaged = (label: string) => {
  const next = new Set(stagedSet)
  if (next.has(label)) next.delete(label)
  else next.add(label)
  onStage!(block.questionId, { selected: Array.from(next), mode: 'option' })
}
const toggleLocal = (label: string) =>
  setLocalChecked((prev) => {
    const next = new Set(prev)
    if (next.has(label)) next.delete(label)
    else next.add(label)
    return next
  })

return (
  <div role="group" aria-labelledby={headerId} className={containerClass}>
    {heading}
    <MultiSelectOptions
      block={block}
      checked={grouped ? stagedSet : localChecked}
      onToggle={grouped ? toggleStaged : toggleLocal}
      onSubmit={grouped ? undefined : () => onAnswer(block.questionId, Array.from(localChecked))}
      showSubmit={!grouped}
    />
    {escapeHatches}
  </div>
)
```

Note: the `stagedSet`/`toggleStaged`/`toggleLocal` declarations must sit before this `return` in the component body (after `grouped` is computed). Keep them out of the single-select early-return path (they are only referenced here).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx`
Expected: PASS — grouped multi-select tests pass; existing uncontrolled multi-select tests (internal `checked`, "Submit" click, keyboard digit/Space + Enter submit) still pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/QuestionCard.tsx web/src/components/session/__tests__/QuestionCard.test.tsx
git commit -m "feat(web): support staged (controlled) multi-select in QuestionCard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: QuestionGroup component (batch + single submit)

**Files:**
- Create: `web/src/components/session/QuestionGroup.tsx`
- Test: `web/src/components/session/__tests__/QuestionGroup.test.tsx`

**Interfaces:**
- Consumes: `QuestionCard`, `StagedAnswer`; `QuestionBlock = Extract<Block, { kind: 'question' }>`.
- Produces: `export function QuestionGroup(props: { questions: QuestionBlock[]; onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void }): JSX.Element`.
- Behavior:
  - holds a local `Map<string, StagedAnswer>` of staged answers keyed by `questionId`.
  - renders one `QuestionCard` per question (with `key={q.questionId}`, `onStage`, and `staged`), and one "Submit all answers" button.
  - "Submit all answers" is `disabled` until every question has a staged answer with non-empty `selected`.
  - on submit, calls `onAnswer(questionId, selected, mode)` once per question, in `questions` order.

- [ ] **Step 1: Write the failing tests**

Create `web/src/components/session/__tests__/QuestionGroup.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QuestionGroup } from '../QuestionGroup'
import type { Block } from '@/session/reducer'

type QuestionBlock = Extract<Block, { kind: 'question' }>

const mk = (id: string, multiSelect = false): QuestionBlock => ({
  kind: 'question', id, questionId: id, header: `H-${id}`, question: `${id}?`,
  options: [{ label: 'Yes' }, { label: 'No' }], multiSelect, live: true,
})

describe('QuestionGroup', () => {
  it('renders every question in the group', () => {
    render(<QuestionGroup questions={[mk('a'), mk('b')]} onAnswer={vi.fn()} />)
    expect(screen.getByText('a?')).toBeInTheDocument()
    expect(screen.getByText('b?')).toBeInTheDocument()
  })

  it('disables submit until all questions are answered', async () => {
    const user = userEvent.setup()
    render(<QuestionGroup questions={[mk('a'), mk('b')]} onAnswer={vi.fn()} />)
    const submit = screen.getByRole('button', { name: /submit all answers/i })
    expect(submit).toBeDisabled()
    await user.click(screen.getByRole('radio', { name: 'Yes' }))   // answer 'a'
    expect(submit).toBeDisabled()                                   // 'b' still open
    await user.click(screen.getAllByRole('radio', { name: 'No' })[1]) // answer 'b'
    expect(submit).toBeEnabled()
  })

  it('sends one onAnswer per question on submit, in order', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionGroup questions={[mk('a'), mk('b')]} onAnswer={onAnswer} />)
    await user.click(screen.getAllByRole('radio', { name: 'Yes' })[0]) // a → Yes
    await user.click(screen.getAllByRole('radio', { name: 'No' })[1])  // b → No
    await user.click(screen.getByRole('button', { name: /submit all answers/i }))
    expect(onAnswer).toHaveBeenCalledTimes(2)
    expect(onAnswer).toHaveBeenNthCalledWith(1, 'a', ['Yes'], 'option')
    expect(onAnswer).toHaveBeenNthCalledWith(2, 'b', ['No'], 'option')
  })

  it('lets the user change an answer before submitting', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionGroup questions={[mk('a')]} onAnswer={onAnswer} />)
    await user.click(screen.getByRole('radio', { name: 'Yes' }))
    await user.click(screen.getByRole('radio', { name: 'No' }))  // change before submit
    await user.click(screen.getByRole('button', { name: /submit all answers/i }))
    expect(onAnswer).toHaveBeenCalledTimes(1)
    expect(onAnswer).toHaveBeenCalledWith('a', ['No'], 'option')
  })

  it('treats an empty multi-select as unanswered', async () => {
    const user = userEvent.setup()
    render(<QuestionGroup questions={[mk('m', true)]} onAnswer={vi.fn()} />)
    const submit = screen.getByRole('button', { name: /submit all answers/i })
    expect(submit).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /yes/i }))   // now 1 selected
    expect(submit).toBeEnabled()
    await user.click(screen.getByRole('checkbox', { name: /yes/i }))   // back to empty
    expect(submit).toBeDisabled()
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/QuestionGroup.test.tsx`
Expected: FAIL — module `../QuestionGroup` does not exist.

- [ ] **Step 3: Implement QuestionGroup**

Create `web/src/components/session/QuestionGroup.tsx`:

```tsx
import { useState } from 'react'
import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { QuestionCard, type StagedAnswer } from './QuestionCard'

type QuestionBlock = Extract<Block, { kind: 'question' }>

interface Props {
  questions: QuestionBlock[]
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
}

/**
 * Renders all questions from a single AskUserQuestion tool call together and
 * batches their answers behind one submit. Answers are staged locally so the
 * user can revisit and change any of them before committing; nothing is sent
 * until every question is answered and "Submit all answers" is pressed.
 */
export function QuestionGroup({ questions, onAnswer }: Props) {
  const [answers, setAnswers] = useState<Map<string, StagedAnswer>>(new Map())

  const stage = (questionId: string, answer: StagedAnswer) =>
    setAnswers((prev) => {
      const next = new Map(prev)
      next.set(questionId, answer)
      return next
    })

  const allAnswered = questions.every((q) => {
    const a = answers.get(q.questionId)
    return a !== undefined && a.selected.length > 0
  })

  const submitAll = () => {
    for (const q of questions) {
      const a = answers.get(q.questionId)
      if (a) onAnswer(q.questionId, a.selected, a.mode)
    }
  }

  return (
    <div className="space-y-2">
      {questions.map((q) => (
        <QuestionCard
          key={q.questionId}
          block={q}
          onAnswer={onAnswer}
          staged={answers.get(q.questionId) ?? null}
          onStage={stage}
        />
      ))}
      <button
        type="button"
        disabled={!allAnswered}
        onClick={submitAll}
        className={cn(
          'rounded px-4 py-2 text-sm font-medium min-h-[44px]',
          'bg-blue-600 text-white hover:bg-blue-700',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          'disabled:opacity-50 disabled:cursor-not-allowed',
        )}
      >
        Submit all answers
      </button>
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/QuestionGroup.test.tsx`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/QuestionGroup.tsx web/src/components/session/__tests__/QuestionGroup.test.tsx
git commit -m "feat(web): add QuestionGroup for batched multi-question submit

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Wire PendingQuestionPanel to group the latest turn's live questions

**Files:**
- Modify: `web/src/components/session/PendingQuestionPanel.tsx`
- Test: `web/src/components/session/__tests__/PendingQuestionPanel.test.tsx`

**Interfaces:**
- Consumes: `QuestionGroup` (Task 5), `QuestionCard`.
- Produces: `PendingQuestionPanel` now renders a single immediate-send `QuestionCard` when exactly one live question exists in the latest turn that has any, or a `QuestionGroup` when two or more do. Props unchanged.

- [ ] **Step 1: Update the existing "one at a time" test and add grouping tests**

In `PendingQuestionPanel.test.tsx`, the old "one at a time" expectation no longer holds (we now show all questions of a turn together). Replace the test named `'one at a time: shows only the first live question; the second after the first is gone'` with:

```tsx
it('shows all live questions of a turn together and batches submit', async () => {
  const user = userEvent.setup()
  const onAnswer = vi.fn()
  render(
    <PendingQuestionPanel turns={[turn(q('a', true), q('b', true))]} onAnswer={onAnswer} onChat={vi.fn()} armedQuestionId={null} />)
  // Both questions are visible at once.
  expect(screen.getByText('a?')).toBeInTheDocument()
  expect(screen.getByText('b?')).toBeInTheDocument()
  // Nothing is sent until the group submit fires.
  const submit = screen.getByRole('button', { name: /submit all answers/i })
  expect(submit).toBeDisabled()
  await user.click(screen.getAllByRole('radio', { name: 'Yes' })[0]) // a
  await user.click(screen.getAllByRole('radio', { name: 'Yes' })[1]) // b
  expect(onAnswer).not.toHaveBeenCalled()
  await user.click(submit)
  expect(onAnswer).toHaveBeenCalledTimes(2)
})
```

(The single-question tests — immediate send, chat-arm, freeform, region role — stay as-is and must keep passing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && node_modules/.bin/vitest run src/components/session/__tests__/PendingQuestionPanel.test.tsx -t "all live questions of a turn"`
Expected: FAIL — only the first question renders; no "Submit all answers" button (panel still pins one at a time).

- [ ] **Step 3: Implement grouping in PendingQuestionPanel**

Rewrite `PendingQuestionPanel.tsx`:

```tsx
import type { Turn, Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { QuestionCard } from './QuestionCard'
import { QuestionGroup } from './QuestionGroup'

type QuestionBlock = Extract<Block, { kind: 'question' }>

interface Props {
  turns: Turn[]
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  onChat: (questionId: string) => void
  armedQuestionId: string | null
}

/**
 * All live, open questions belonging to the latest turn that has any. Questions
 * from one AskUserQuestion call share a turn, so this returns them as a group.
 */
function liveGroup(turns: Turn[]): QuestionBlock[] {
  let group: QuestionBlock[] = []
  for (const t of turns) {
    const live = t.blocks.filter(
      (b): b is QuestionBlock => b.kind === 'question' && !!b.live,
    )
    if (live.length > 0) group = live
  }
  return group
}

export function PendingQuestionPanel({ turns, onAnswer, onChat, armedQuestionId }: Props) {
  const group = liveGroup(turns)
  if (group.length === 0) return null
  return (
    <div
      role="region"
      aria-label="Pending question"
      aria-live="polite"
      className={cn(
        'flex-shrink-0 border-t border-amber-200 dark:border-amber-800',
        'bg-amber-50/60 dark:bg-amber-950/20 px-3 py-2',
      )}
      data-testid="pending-question-panel"
    >
      {group.length === 1 ? (
        <QuestionCard
          key={group[0].questionId}
          block={group[0]}
          onAnswer={onAnswer}
          onChat={onChat}
          armedForChat={group[0].questionId === armedQuestionId}
        />
      ) : (
        <QuestionGroup questions={group} onAnswer={onAnswer} />
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run the full session test suite to verify it passes**

Run: `cd web && node_modules/.bin/vitest run src/components/session/`
Expected: PASS — `PendingQuestionPanel`, `QuestionCard`, `QuestionGroup` suites all green. The carryover test from Task 2 still passes (single-question path keeps its `key`).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/PendingQuestionPanel.tsx web/src/components/session/__tests__/PendingQuestionPanel.test.tsx
git commit -m "feat(web): group a turn's live questions into a single batched panel

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Full gates + branch finish

**Files:** none (verification + integration).

**Interfaces:** none.

- [ ] **Step 1: Type-check (the real gate)**

Run: `cd web && node_modules/.bin/tsc -b --force`
Expected: no errors. (`tsc --noEmit` checks nothing in this repo — `tsc -b` is the gate.)

- [ ] **Step 2: Lint with zero warnings**

Run: `cd web && node_modules/.bin/eslint . --max-warnings 0`
Expected: clean. Watch for the react-hooks "no ref read during render" rule and unused-var warnings.

- [ ] **Step 3: Full web test run**

Run: `cd web && node_modules/.bin/vitest run`
Expected: all suites PASS (no regressions in `Turn`, `SessionBody`, or other consumers of `QuestionCard`).

- [ ] **Step 4: Confirm no backend change crept in**

Run: `git diff --name-only origin/master...HEAD`
Expected: only files under `web/src/components/session/` and `docs/superpowers/`. If anything under `server/` or `src/` appears, stop and review.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin feat/grouped-multi-question-panel
gh pr create --title "Grouped multi-question panel: descriptions, batched submit, no draft carryover" \
  --body "$(cat <<'EOF'
## Summary
- Render per-option `description` text in `QuestionCard` (single, multi, read-only). Data already arrived on the wire; the UI just dropped it.
- New `QuestionGroup`: when one AskUserQuestion call asks 2+ questions, show them all at once, stage answers locally so any can be revisited/changed, and commit them behind one "Submit all answers" (disabled until all answered). Single-question prompts stay immediate-send.
- Fix freeform-reply carryover: `key={questionId}` remounts `QuestionCard` per question so a typed draft never leaks into the next one.

No backend/protocol changes — the driver already forwards descriptions and `asyncio.gather`s all answer futures.

Spec: `docs/superpowers/specs/2026-06-23-grouped-multi-question-panel-design.md`
Plan: `docs/superpowers/plans/2026-06-23-grouped-multi-question-panel.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

Expected: PR opens non-draft with auto-merge armed; CI `frontend` + `lint-and-test` run.

---

## Self-Review notes

- **Spec coverage:** descriptions (Task 1), batched/revisitable submit (Tasks 3–6), carryover fix (Task 2), single-question unchanged (Task 6 branch), Submit-disabled-until-all-answered (Task 5). Grouped chat hatch intentionally dropped (documented in Task 3 — chat is a whole-turn bail-out that doesn't compose with N pending futures); single-question chat unchanged.
- **Type consistency:** `StagedAnswer = { selected: string[]; mode: Exclude<AnswerMode,'chat'> }` defined and exported in Task 3, consumed by Tasks 4–5. `onStage(questionId, answer)` signature consistent across QuestionCard/QuestionGroup. `onAnswer` signature never changes.
- **No placeholders:** every code/test step shows complete code and exact run commands.
