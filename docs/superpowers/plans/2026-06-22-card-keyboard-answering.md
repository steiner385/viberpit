# Card Keyboard Answering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a keyboard user answer in-session permission and question cards with number keys, arrow keys, and Enter — card-focused, no mouse.

**Architecture:** One shared `useCardKeyboard` hook owns all roving-focus + key logic; `PermissionCard` calls it directly, and `QuestionCard`'s interactive option lists are extracted into `SingleSelectOptions`/`MultiSelectOptions` child components that own the hook (so it's never called from a conditional branch — `QuestionCard` early-returns for `readOnly`/`armedForChat`). Multi-select options become `role="checkbox"` buttons (no native `<input>`) so every key flows through the hook.

**Tech Stack:** React + TypeScript; Vitest + @testing-library/react + @testing-library/user-event; Tailwind.

## Global Constraints

- Branch from `origin/master`: `feat/card-keyboard-answering` (already created).
- TDD: failing test first → watch it fail → minimal implementation → watch it pass → commit.
- Single source of truth (constitution VI): all option-list key behavior lives in `useCardKeyboard`; do not duplicate key handling in either card.
- Key scheme: **numbers + arrows + Enter**. Digit `n` (1–9) → option `n-1` (ignored if ≥ count). `↑/↓` move a wrapping highlight. Single-select: Enter/Space activate the highlighted option. Multi-select: Enter = **Submit**, Space = toggle highlighted.
- Scope: **card-focused** — the hook auto-focuses option 0 on mount; keys are handled on the option container's `onKeyDown`.
- Handled keys call `e.preventDefault()` AND `e.stopPropagation()` (so arrows/Enter never bubble to SessionView's window-level Esc/ArrowLeft/Shift+Tab handlers).
- The hook **bails when `e.target` is an `INPUT` or `TEXTAREA`** (protects the freeform "Write a response" textarea).
- Escape hatches (Write a response / Chat about this) stay Tab-reachable but are NOT in the number/arrow scheme.
- Existing card tests must stay green (PermissionCard finds buttons by aria-label; QuestionCard expects `getByRole('radiogroup')` and `getAllByRole('checkbox')` length 2).
- Verification (run in `web/`): `node_modules/.bin/vitest run`, `node_modules/.bin/tsc --noEmit`, `node_modules/.bin/eslint .` — all green before each commit.
- Env: `web/node_modules` is a symlink to a prebuilt checkout (already set up). Do NOT run `npm install`.

---

## Task 1: `useCardKeyboard` hook

**Files:**
- Create: `web/src/components/session/useCardKeyboard.ts`
- Test: `web/src/components/session/__tests__/useCardKeyboard.test.tsx`

**Interfaces:**
- Produces:
  ```ts
  interface CardKeyboardOptions {
    count: number
    multiSelect?: boolean
    onActivate: (index: number) => void
    onToggle?: (index: number) => void
    onSubmit?: () => void
  }
  interface CardKeyboardApi {
    activeIndex: number
    containerProps: { onKeyDown: (e: React.KeyboardEvent) => void }
    itemProps: (index: number) => { ref: (el: HTMLElement | null) => void; tabIndex: number }
  }
  function useCardKeyboard(opts: CardKeyboardOptions): CardKeyboardApi
  ```

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/useCardKeyboard.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useCardKeyboard, type CardKeyboardOptions } from '../useCardKeyboard'

function Harness(opts: CardKeyboardOptions & { withInput?: boolean }) {
  const kb = useCardKeyboard(opts)
  return (
    <div data-testid="container" {...kb.containerProps}>
      {opts.withInput && <input aria-label="freeform" />}
      {Array.from({ length: opts.count }).map((_, i) => (
        <button key={i} {...kb.itemProps(i)} data-testid={`item-${i}`}>opt{i}</button>
      ))}
      <span data-testid="active">{kb.activeIndex}</span>
    </div>
  )
}

describe('useCardKeyboard', () => {
  it('focuses item 0 on mount', () => {
    render(<Harness count={3} onActivate={vi.fn()} />)
    expect(screen.getByTestId('item-0')).toHaveFocus()
  })

  it('ArrowDown/ArrowUp move the highlight with wraparound', async () => {
    const user = userEvent.setup()
    render(<Harness count={3} onActivate={vi.fn()} />)
    await user.keyboard('{ArrowDown}')
    expect(screen.getByTestId('active')).toHaveTextContent('1')
    await user.keyboard('{ArrowUp}{ArrowUp}') // 1 -> 0 -> wrap to 2
    expect(screen.getByTestId('active')).toHaveTextContent('2')
  })

  it('digit key activates that option (single-select)', async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    render(<Harness count={3} onActivate={onActivate} />)
    await user.keyboard('2')
    expect(onActivate).toHaveBeenCalledWith(1)
  })

  it('ignores a digit greater than count', async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    render(<Harness count={2} onActivate={onActivate} />)
    await user.keyboard('5')
    expect(onActivate).not.toHaveBeenCalled()
  })

  it('Enter activates the highlighted option (single-select)', async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    render(<Harness count={3} onActivate={onActivate} />)
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onActivate).toHaveBeenCalledWith(1)
  })

  it('multi-select: digit/Space toggle, Enter submits', async () => {
    const user = userEvent.setup()
    const onToggle = vi.fn()
    const onSubmit = vi.fn()
    render(<Harness count={3} multiSelect onActivate={vi.fn()} onToggle={onToggle} onSubmit={onSubmit} />)
    await user.keyboard('3')            // toggle index 2
    expect(onToggle).toHaveBeenCalledWith(2)
    await user.keyboard(' ')            // toggle highlighted (0)
    expect(onToggle).toHaveBeenCalledWith(0)
    await user.keyboard('{Enter}')
    expect(onSubmit).toHaveBeenCalledOnce()
  })

  it('bails when the event target is an INPUT', async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    render(<Harness count={3} withInput onActivate={onActivate} />)
    await user.click(screen.getByLabelText('freeform'))
    await user.keyboard('2')
    expect(onActivate).not.toHaveBeenCalled()
    expect(screen.getByLabelText('freeform')).toHaveValue('2')
  })
})
```

- [ ] **Step 2: Run it (red)**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/useCardKeyboard.test.tsx`
Expected: FAIL — module `../useCardKeyboard` not found.

- [ ] **Step 3: Implement the hook**

Create `web/src/components/session/useCardKeyboard.ts`:

```ts
import { useCallback, useEffect, useRef, useState } from 'react'

export interface CardKeyboardOptions {
  count: number
  multiSelect?: boolean
  onActivate: (index: number) => void
  onToggle?: (index: number) => void
  onSubmit?: () => void
}

export interface CardKeyboardApi {
  activeIndex: number
  containerProps: { onKeyDown: (e: React.KeyboardEvent) => void }
  itemProps: (index: number) => { ref: (el: HTMLElement | null) => void; tabIndex: number }
}

/**
 * Roving-focus keyboard handling for a card's option list (permission/question
 * cards). Numbers pick an option, arrows move a wrapping highlight, Enter/Space
 * activate (single-select) or toggle/submit (multi-select). Bails on INPUT/TEXTAREA
 * targets so freeform typing is never hijacked.
 */
export function useCardKeyboard({
  count, multiSelect = false, onActivate, onToggle, onSubmit,
}: CardKeyboardOptions): CardKeyboardApi {
  const [activeIndex, setActiveIndex] = useState(0)
  const itemRefs = useRef<(HTMLElement | null)[]>([])

  useEffect(() => {
    itemRefs.current[0]?.focus()
  }, [])

  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    const tag = (e.target as HTMLElement).tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA') return
    if (count === 0) return

    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); e.stopPropagation()
      const next = e.key === 'ArrowDown'
        ? (activeIndex + 1) % count
        : (activeIndex - 1 + count) % count
      setActiveIndex(next)
      itemRefs.current[next]?.focus()
      return
    }
    if (/^[1-9]$/.test(e.key)) {
      const n = Number(e.key) - 1
      if (n < count) {
        e.preventDefault(); e.stopPropagation()
        if (multiSelect) onToggle?.(n)
        else onActivate(n)
      }
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault(); e.stopPropagation()
      if (multiSelect) onSubmit?.()
      else onActivate(activeIndex)
      return
    }
    if (e.key === ' ' || e.key === 'Spacebar') {
      e.preventDefault(); e.stopPropagation()
      if (multiSelect) onToggle?.(activeIndex)
      else onActivate(activeIndex)
    }
  }, [count, multiSelect, onActivate, onToggle, onSubmit, activeIndex])

  const itemProps = useCallback((index: number) => ({
    ref: (el: HTMLElement | null) => { itemRefs.current[index] = el },
    tabIndex: index === activeIndex ? 0 : -1,
  }), [activeIndex])

  return { activeIndex, containerProps: { onKeyDown }, itemProps }
}
```

- [ ] **Step 4: Run it (green)**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/useCardKeyboard.test.tsx`
Expected: PASS (all 7).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/useCardKeyboard.ts web/src/components/session/__tests__/useCardKeyboard.test.tsx
git commit -m "feat(session): useCardKeyboard hook for card option-list navigation"
```

---

## Task 2: Wire PermissionCard

**Files:**
- Modify: `web/src/components/session/PermissionCard.tsx`
- Test: `web/src/components/session/__tests__/PermissionCard.test.tsx` (extend)

**Interfaces:**
- Consumes: `useCardKeyboard` (Task 1).

- [ ] **Step 1: Write the failing tests**

Append inside `describe('PermissionCard', …)` in `web/src/components/session/__tests__/PermissionCard.test.tsx` (reuse the file's existing `bashPerm`/`planPerm` fixtures):

```tsx
it('digit 1 allows, 2 denies, 3 always (normal mode)', async () => {
  const user = userEvent.setup()
  const onDecision = vi.fn()
  render(<PermissionCard block={bashPerm} onDecision={onDecision} />)
  await user.keyboard('1')
  expect(onDecision).toHaveBeenCalledWith('perm-1', 'allow')
  await user.keyboard('2')
  expect(onDecision).toHaveBeenCalledWith('perm-1', 'deny')
  await user.keyboard('3')
  expect(onDecision).toHaveBeenCalledWith('perm-1', 'always')
})

it('digit 3 is ignored in interactive mode (no always button)', async () => {
  const user = userEvent.setup()
  const onDecision = vi.fn()
  render(<PermissionCard block={bashPerm} mode="interactive" onDecision={onDecision} />)
  await user.keyboard('3')
  expect(onDecision).not.toHaveBeenCalled()
})

it('ArrowDown then Enter denies', async () => {
  const user = userEvent.setup()
  const onDecision = vi.fn()
  render(<PermissionCard block={bashPerm} onDecision={onDecision} />)
  await user.keyboard('{ArrowDown}{Enter}')
  expect(onDecision).toHaveBeenCalledWith('perm-1', 'deny')
})

it('plan variant: digit 1 allows, 2 denies', async () => {
  const user = userEvent.setup()
  const onDecision = vi.fn()
  render(<PermissionCard block={planPerm} onDecision={onDecision} />)
  await user.keyboard('1')
  expect(onDecision).toHaveBeenCalledWith(planPerm.permId, 'allow')
  await user.keyboard('2')
  expect(onDecision).toHaveBeenCalledWith(planPerm.permId, 'deny')
})
```

- [ ] **Step 2: Run them (red)**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/PermissionCard.test.tsx -t "digit|ArrowDown|plan variant: digit"`
Expected: FAIL — no key handling yet.

- [ ] **Step 3: Rewrite PermissionCard to use the hook**

Replace the whole body of `web/src/components/session/PermissionCard.tsx` with:

```tsx
import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { useCardKeyboard } from './useCardKeyboard'

type PermBlock = Extract<Block, { kind: 'permission' }>
type Decision = 'allow' | 'deny' | 'always'

interface Props {
  block: PermBlock
  /** The session permission mode (e.g. 'interactive', 'auto', 'plan'). */
  mode?: string
  onDecision: (permId: string, decision: Decision) => void
}

function keyArgSummary(block: PermBlock): string {
  const { toolKind, input } = block
  if (toolKind === 'bash' && typeof input.command === 'string') return input.command.slice(0, 60)
  if ((toolKind === 'edit' || toolKind === 'write' || toolKind === 'read') && typeof input.file_path === 'string') return input.file_path
  return ''
}

const primaryBtn = cn(
  'min-h-[48px] rounded px-4 py-2 text-sm font-semibold',
  'bg-blue-600 text-white hover:bg-blue-700',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
)
const allowBtn = cn(
  'min-h-[48px] rounded px-4 py-2 text-sm font-medium',
  'bg-green-600 text-white hover:bg-green-700',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
)
const secondaryBtn = cn(
  'min-h-[48px] rounded px-4 py-2 text-sm',
  'bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600',
  'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
)

export function PermissionCard({ block, mode, onDecision }: Props) {
  const summary = keyArgSummary(block)
  const toolLabel = summary ? `${block.tool}: ${summary}` : block.tool
  const isInteractive = mode === 'interactive'

  // Ordered actions matching the rendered buttons; digit/arrow indices map here.
  const actions: Decision[] = block.plan
    ? ['allow', 'deny']
    : isInteractive
      ? ['allow', 'deny']
      : ['allow', 'deny', 'always']

  const kb = useCardKeyboard({
    count: actions.length,
    onActivate: (i) => onDecision(block.permId, actions[i]),
  })

  return (
    <div
      role="group"
      aria-label={`Permission request: ${toolLabel}`}
      className="rounded border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/30 p-3 my-1"
    >
      <div className="mb-2 text-sm font-medium text-gray-900 dark:text-gray-100">
        {block.plan ? 'Plan step approval' : 'Permission request'}
      </div>
      <div className="mb-3 text-xs text-gray-700 dark:text-gray-300 font-mono truncate">
        {toolLabel}
      </div>

      <div className="flex flex-col sm:flex-row gap-2" {...kb.containerProps}>
        {block.plan ? (
          <>
            <button type="button" {...kb.itemProps(0)} onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Approve & run ${toolLabel}`} className={primaryBtn}>
              Approve & run
            </button>
            <button type="button" {...kb.itemProps(1)} onClick={() => onDecision(block.permId, 'deny')}
              aria-label={`Keep planning ${toolLabel}`} className={secondaryBtn}>
              Keep planning
            </button>
          </>
        ) : (
          <>
            <button type="button" {...kb.itemProps(0)} onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Allow ${toolLabel}`} className={allowBtn}>
              Yes
            </button>
            <button type="button" {...kb.itemProps(1)} onClick={() => onDecision(block.permId, 'deny')}
              aria-label={`Deny ${toolLabel}`} className={secondaryBtn}>
              No
            </button>
            {!isInteractive && (
              <button type="button" {...kb.itemProps(2)} onClick={() => onDecision(block.permId, 'always')}
                aria-label={`Always allow ${toolLabel}`} className={primaryBtn}>
                Yes, always
              </button>
            )}
          </>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run the full PermissionCard suite (green)**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/PermissionCard.test.tsx`
Expected: PASS — new key tests plus all pre-existing click/label/mode tests.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/PermissionCard.tsx web/src/components/session/__tests__/PermissionCard.test.tsx
git commit -m "feat(permission-card): keyboard answering (1/2/3, arrows, Enter)"
```

---

## Task 3: QuestionCard single-select keyboard

**Files:**
- Modify: `web/src/components/session/QuestionCard.tsx`
- Test: `web/src/components/session/__tests__/QuestionCard.test.tsx` (extend)

**Interfaces:**
- Consumes: `useCardKeyboard` (Task 1).
- Produces: internal `SingleSelectOptions` component (not exported).

- [ ] **Step 1: Write the failing tests**

Append inside `describe('QuestionCard', …)` (reuse the existing `singleQ` fixture with options `Option A`/`Option B`):

```tsx
it('single-select: digit 2 answers with the second option', async () => {
  const user = userEvent.setup()
  const onAnswer = vi.fn()
  render(<QuestionCard block={singleQ} onAnswer={onAnswer} />)
  await user.keyboard('2')
  expect(onAnswer).toHaveBeenCalledWith('q-1', ['Option B'])
})

it('single-select: ArrowDown then Enter answers the highlighted option', async () => {
  const user = userEvent.setup()
  const onAnswer = vi.fn()
  render(<QuestionCard block={singleQ} onAnswer={onAnswer} />)
  await user.keyboard('{ArrowDown}{Enter}')
  expect(onAnswer).toHaveBeenCalledWith('q-1', ['Option B'])
})

it('single-select: first option is focused on mount', () => {
  render(<QuestionCard block={singleQ} onAnswer={vi.fn()} />)
  expect(screen.getByRole('radio', { name: 'Option A' })).toHaveFocus()
})
```

- [ ] **Step 2: Run them (red)**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx -t "single-select: digit|single-select: ArrowDown|first option is focused"`
Expected: FAIL — no key handling / no autofocus.

- [ ] **Step 3: Extract `SingleSelectOptions` and use it**

In `web/src/components/session/QuestionCard.tsx`:

(a) add the import at the top:

```tsx
import { useCardKeyboard } from './useCardKeyboard'
```

(b) add this component above `export function QuestionCard`:

```tsx
function SingleSelectOptions({
  block, onAnswer,
}: { block: QuestionBlock; onAnswer: Props['onAnswer'] }) {
  const headerId = `q-header-${block.questionId}`
  const kb = useCardKeyboard({
    count: block.options.length,
    onActivate: (i) => onAnswer(block.questionId, [optionLabel(block.options[i])]),
  })
  return (
    <div role="radiogroup" aria-labelledby={headerId} className="space-y-1" {...kb.containerProps}>
      {block.options.map((opt, i) => {
        const label = optionLabel(opt)
        return (
          <button
            key={i}
            type="button"
            role="radio"
            aria-checked={i === kb.activeIndex}
            {...kb.itemProps(i)}
            onClick={() => onAnswer(block.questionId, [label])}
            className={cn(
              'w-full text-left rounded px-3 py-2 text-sm',
              'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
              'text-gray-800 dark:text-gray-200',
              'hover:bg-blue-50 dark:hover:bg-blue-900/30',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              'min-h-[44px]',
            )}
          >
            {label}
          </button>
        )
      })}
    </div>
  )
}
```

(c) replace the existing single-select `return` block (the `if (!block.multiSelect) { return ( <div role="radiogroup" …> … </div> ) }`) with:

```tsx
if (!block.multiSelect) {
  return (
    <div role="group" aria-labelledby={headerId} className={containerClass}>
      {heading}
      <SingleSelectOptions block={block} onAnswer={onAnswer} />
      {escapeHatches}
    </div>
  )
}
```

- [ ] **Step 4: Run the full QuestionCard suite (green)**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx`
Expected: PASS — new key tests plus the existing single-select click/role tests (`getByRole('radiogroup')`, `getByRole('radio', {name})`).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/QuestionCard.tsx web/src/components/session/__tests__/QuestionCard.test.tsx
git commit -m "feat(question-card): single-select keyboard answering"
```

---

## Task 4: QuestionCard multi-select keyboard

**Files:**
- Modify: `web/src/components/session/QuestionCard.tsx`
- Test: `web/src/components/session/__tests__/QuestionCard.test.tsx` (extend)

**Interfaces:**
- Consumes: `useCardKeyboard` (Task 1).
- Produces: internal `MultiSelectOptions` component (not exported). Converts options to `role="checkbox"` buttons (no native `<input>`).

- [ ] **Step 1: Write the failing tests**

Append inside `describe('QuestionCard', …)` (reuse the existing `multiQ` fixture with options `Option A`/`Option B`):

```tsx
it('multi-select: digit toggles aria-checked', async () => {
  const user = userEvent.setup()
  render(<QuestionCard block={multiQ} onAnswer={vi.fn()} />)
  const optA = screen.getByRole('checkbox', { name: /option a/i })
  expect(optA).toHaveAttribute('aria-checked', 'false')
  await user.keyboard('1')
  expect(optA).toHaveAttribute('aria-checked', 'true')
})

it('multi-select: digit-toggle then Enter submits the checked set', async () => {
  const user = userEvent.setup()
  const onAnswer = vi.fn()
  render(<QuestionCard block={multiQ} onAnswer={onAnswer} />)
  await user.keyboard('1')        // toggle Option A
  await user.keyboard('{Enter}')  // submit
  expect(onAnswer).toHaveBeenCalledWith('q-2', ['Option A'])
})

it('multi-select: Space toggles the highlighted option', async () => {
  const user = userEvent.setup()
  const onAnswer = vi.fn()
  render(<QuestionCard block={multiQ} onAnswer={onAnswer} />)
  await user.keyboard(' ')        // toggle highlighted (Option A)
  await user.keyboard('{Enter}')
  expect(onAnswer).toHaveBeenCalledWith('q-2', ['Option A'])
})

it('multi-select: typing a digit into the freeform textarea does not toggle', async () => {
  const user = userEvent.setup()
  render(<QuestionCard block={multiQ} onAnswer={vi.fn()} />)
  await user.click(screen.getByRole('button', { name: /write a response/i }))
  const textarea = screen.getByRole('textbox', { name: /write a response/i })
  await user.type(textarea, '2')
  expect(textarea).toHaveValue('2')
  expect(screen.getByRole('checkbox', { name: /option b/i })).toHaveAttribute('aria-checked', 'false')
})
```

- [ ] **Step 2: Run them (red)**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx -t "multi-select: digit|multi-select: Space|multi-select: typing"`
Expected: FAIL — native checkboxes, no key handling.

- [ ] **Step 3: Extract `MultiSelectOptions` and use it**

In `web/src/components/session/QuestionCard.tsx`:

(a) add this component above `export function QuestionCard` (it owns the `checked` state, which moves out of `QuestionCard`):

```tsx
function MultiSelectOptions({
  block, onAnswer,
}: { block: QuestionBlock; onAnswer: Props['onAnswer'] }) {
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const toggle = (label: string) =>
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(label)) next.delete(label)
      else next.add(label)
      return next
    })
  const kb = useCardKeyboard({
    count: block.options.length,
    multiSelect: true,
    onToggle: (i) => toggle(optionLabel(block.options[i])),
    onSubmit: () => onAnswer(block.questionId, Array.from(checked)),
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
              onClick={() => toggle(label)}
              className={cn(
                'flex items-center gap-2 w-full text-left rounded px-3 py-2 text-sm',
                'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
                'text-gray-800 dark:text-gray-200',
                'hover:bg-blue-50 dark:hover:bg-blue-900/30',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                'min-h-[44px]',
              )}
            >
              <span aria-hidden="true" className="inline-block w-4">{isChecked ? '☑' : '☐'}</span>
              <span>{label}</span>
            </button>
          )
        })}
      </div>
      <button
        type="button"
        onClick={() => onAnswer(block.questionId, Array.from(checked))}
        className={cn(
          'rounded px-4 py-2 text-sm font-medium',
          'bg-blue-600 text-white hover:bg-blue-700',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          'min-h-[44px]',
        )}
      >
        Submit
      </button>
    </>
  )
}
```

(b) replace the existing multi-select `return` block (the final `return ( <div role="group" …> {heading} <div className="space-y-1 mb-3"> …checkboxes… </div> <button …>Submit</button> {escapeHatches} </div> )`) with:

```tsx
return (
  <div role="group" aria-labelledby={headerId} className={containerClass}>
    {heading}
    <MultiSelectOptions block={block} onAnswer={onAnswer} />
    {escapeHatches}
  </div>
)
```

(c) remove the now-unused top-level `checked`/`setChecked` `useState` from `QuestionCard` (it moved into `MultiSelectOptions`). Keep the `writing`/`draft` state — the escape hatches still use it. Confirm `useState` is still imported (it is, for `writing`/`draft`).

- [ ] **Step 4: Run the full QuestionCard suite (green)**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/QuestionCard.test.tsx`
Expected: PASS — new key tests plus existing multi-select tests (`getAllByRole('checkbox')` length 2; click checkbox + Submit → `onAnswer('q-2', ['Option A'])`).

- [ ] **Step 5: Run tsc + eslint**

Run (in `web/`): `node_modules/.bin/tsc --noEmit && node_modules/.bin/eslint .`
Expected: both clean (watch for an unused-import or unused-state warning from the removed `checked`).

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/QuestionCard.tsx web/src/components/session/__tests__/QuestionCard.test.tsx
git commit -m "feat(question-card): multi-select keyboard answering (role=checkbox buttons)"
```

---

## Task 5: Full verification & PR

**Files:** none (integration).

- [ ] **Step 1: Full green gate**

Run (in `web/`): `node_modules/.bin/vitest run && node_modules/.bin/tsc --noEmit && node_modules/.bin/eslint .`
Expected: all pass (820 baseline + new tests).

- [ ] **Step 2: Push & open PR**

```bash
git push -u origin feat/card-keyboard-answering
gh pr create --base master --fill
```

PR body ends with the Claude Code trailer; reference the audit (`P1-a`). Merge once CI (frontend, lint-and-test) is green per the completion workflow.

---

## Self-Review (completed)

- **Spec coverage:** hook → Task 1; PermissionCard → Task 2; QuestionCard single → Task 3; multi (role=checkbox conversion) → Task 4; verification/PR → Task 5. `readOnly`/`armedForChat` keep no hook (the hook lives only in the extracted interactive children) — covered by the extraction in Tasks 3–4. ✓
- **Placeholder scan:** every code step has complete code; no TBD/TODO. ✓
- **Type consistency:** `useCardKeyboard(opts): CardKeyboardApi` defined in Task 1 and consumed with the same option names (`count`, `multiSelect`, `onActivate`, `onToggle`, `onSubmit`) and `containerProps`/`itemProps(i)` in Tasks 2–4. `Decision` type in PermissionCard matches the existing `onDecision` signature. ✓
- **Rules of hooks:** `useCardKeyboard` is called unconditionally inside `PermissionCard`, `SingleSelectOptions`, and `MultiSelectOptions` — never in a branch — so `QuestionCard`'s early returns for `readOnly`/`armedForChat` are safe. ✓
- **Existing tests preserved:** PermissionCard buttons keep their aria-labels; single-select keeps `role="radiogroup"`/`role="radio"`; multi-select keeps `role="checkbox"` (now on buttons) and the Submit button. ✓
