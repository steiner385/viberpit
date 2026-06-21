# Tool-Use Consolidation in Agent View — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce noise in the structured Agent View by rendering tool uses as compact single-line rows and folding long completed runs of tool calls behind a one-line summary, with a global expand/collapse-all toggle.

**Architecture:** Pure render-layer change. A new pure helper `groupBlocks()` coalesces consecutive `tool` blocks in a turn into groups. A new `ToolGroup` component decides whether to show a fold summary or compact rows. `ToolCallCard` gains a `density='line'` mode. A `toolDetailMode` value owned by `SessionView` (`'auto' | 'all-open' | 'all-closed'`) threads down through `TranscriptView → Turn → ToolGroup → ToolCallCard` to allow a global override. No reducer, websocket, or server changes.

**Tech Stack:** React 19 + TypeScript, Vitest + @testing-library/react + @testing-library/user-event, Tailwind, lucide-react icons. Tests run from `web/` with `npm test` (`vitest run`).

## Global Constraints

- All work runs from the `web/` directory. Test command: `npm test` (alias for `vitest run`). Run a single file with `npx vitest run <path>`.
- Mobile-first: interactive controls keep a 44px minimum touch target (`min-h-[44px]`), matching existing components.
- Accessibility: toggles use `aria-expanded` + `aria-controls` pointing at the region they show/hide, matching `ToolCallCard.tsx`.
- Dark mode: every color utility pairs a `dark:` variant, matching existing components.
- TypeScript strict: no `any`; derive tool types via `Extract<Block, { kind: 'tool' }>`.
- Constitution: single source of truth / no dead code (VI). The card-mode layout of `ToolCallCard` is preserved (existing tests depend on it); the new line mode is additive.

---

## File Structure

- Create `web/src/session/groupBlocks.ts` — pure grouping helper + shared `ToolDetailMode`/`RenderItem` types.
- Create `web/src/session/groupBlocks.test.ts` — unit tests for the helper.
- Modify `web/src/components/session/ToolCallCard.tsx` — add `density` prop; new line layout.
- Modify `web/src/components/session/__tests__/ToolCallCard.test.tsx` — pin existing tests to `density="card"`; add line-mode tests.
- Create `web/src/components/session/ToolGroup.tsx` — fold/expand a run of tools.
- Create `web/src/components/session/__tests__/ToolGroup.test.tsx` — component tests.
- Modify `web/src/components/session/Turn.tsx` — group blocks, render `ToolGroup`, accept `isLastTurn` + `toolDetailMode`.
- Modify `web/src/components/session/__tests__/` — add `Turn.test.tsx`.
- Modify `web/src/components/session/TranscriptView.tsx` — accept + pass `toolDetailMode`, mark last turn.
- Modify `web/src/components/session/SessionView.tsx` — own `toolDetailMode`, add toggle button + keybinding.
- Modify `web/src/components/ShortcutsHelp.tsx` — document the new `c` binding.
- Modify `web/src/components/session/__tests__/SessionView.keyboard.test.tsx` (or a new file) — test the `c` toggle.

### Design refinement vs. spec

The spec proposed `ToolCallCard` default `density='line'`. To preserve the existing `ToolCallCard.test.tsx` suite (which asserts the card layout with separate "args"/"result" toggles) and minimize regression risk, the **default stays `'card'`** and **`ToolGroup` always passes `density='line'`**. Because every tool block now routes through `ToolGroup`, the Agent View renders line mode everywhere — the same user-visible outcome the spec intended.

---

## Task 1: `groupBlocks` pure helper

**Files:**
- Create: `web/src/session/groupBlocks.ts`
- Test: `web/src/session/groupBlocks.test.ts`

**Interfaces:**
- Consumes: `Block` from `@/session/reducer`.
- Produces:
  - `type ToolBlock = Extract<Block, { kind: 'tool' }>`
  - `type ToolDetailMode = 'auto' | 'all-open' | 'all-closed'`
  - `type RenderItem = { kind: 'block'; block: Block } | { kind: 'toolGroup'; tools: ToolBlock[] }`
  - `function groupBlocks(blocks: Block[]): RenderItem[]`

- [ ] **Step 1: Write the failing test**

Create `web/src/session/groupBlocks.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { groupBlocks } from './groupBlocks'
import type { Block } from './reducer'

const text = (id: string): Block => ({ kind: 'text', id, text: 'hi' })
const tool = (id: string): Block => ({
  kind: 'tool', id, toolId: id, name: 'Read', toolKind: 'read', input: { file_path: `/f/${id}` },
})

describe('groupBlocks', () => {
  it('returns [] for empty input', () => {
    expect(groupBlocks([])).toEqual([])
  })

  it('passes a lone non-tool block through as a block item', () => {
    const items = groupBlocks([text('t1')])
    expect(items).toEqual([{ kind: 'block', block: text('t1') }])
  })

  it('coalesces consecutive tool blocks into one toolGroup', () => {
    const items = groupBlocks([tool('a'), tool('b'), tool('c')])
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ kind: 'toolGroup' })
    expect(items[0].kind === 'toolGroup' && items[0].tools.map((t) => t.id)).toEqual(['a', 'b', 'c'])
  })

  it('splits tool runs around non-tool blocks, preserving order', () => {
    const items = groupBlocks([tool('a'), text('t1'), tool('b'), tool('c')])
    expect(items.map((i) => i.kind)).toEqual(['toolGroup', 'block', 'toolGroup'])
    expect(items[0].kind === 'toolGroup' && items[0].tools.map((t) => t.id)).toEqual(['a'])
    expect(items[2].kind === 'toolGroup' && items[2].tools.map((t) => t.id)).toEqual(['b', 'c'])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/session/groupBlocks.test.ts`
Expected: FAIL — cannot find module `./groupBlocks`.

- [ ] **Step 3: Write minimal implementation**

Create `web/src/session/groupBlocks.ts`:

```ts
import type { Block } from './reducer'

/** A completed-or-pending tool block (tool is complete iff `result` is defined). */
export type ToolBlock = Extract<Block, { kind: 'tool' }>

/** Global override for how much tool detail to show in the Agent View. */
export type ToolDetailMode = 'auto' | 'all-open' | 'all-closed'

/** A turn's blocks, with consecutive tool blocks coalesced into groups. */
export type RenderItem =
  | { kind: 'block'; block: Block }
  | { kind: 'toolGroup'; tools: ToolBlock[] }

/**
 * Coalesce maximal runs of consecutive `kind:'tool'` blocks into `toolGroup`
 * items; all other blocks pass through as `block` items. Pure, order-preserving.
 */
export function groupBlocks(blocks: Block[]): RenderItem[] {
  const items: RenderItem[] = []
  let run: ToolBlock[] = []

  const flush = () => {
    if (run.length > 0) {
      items.push({ kind: 'toolGroup', tools: run })
      run = []
    }
  }

  for (const block of blocks) {
    if (block.kind === 'tool') {
      run.push(block)
    } else {
      flush()
      items.push({ kind: 'block', block })
    }
  }
  flush()
  return items
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/session/groupBlocks.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/session/groupBlocks.ts web/src/session/groupBlocks.test.ts
git commit -m "feat(session): add groupBlocks helper to coalesce consecutive tool blocks"
```

---

## Task 2: `ToolCallCard` line-density mode

**Files:**
- Modify: `web/src/components/session/ToolCallCard.tsx`
- Test: `web/src/components/session/__tests__/ToolCallCard.test.tsx`

**Interfaces:**
- Consumes: `ToolBlock` (existing local type), `ToolResult`, `kindIcon`/`keyArg`/`sizeHint` (existing helpers in file).
- Produces: `ToolCallCard` now accepts `density?: 'line' | 'card'` (default `'card'`). In `'line'` mode it renders a single toggle row (`aria-label="Toggle tool details"`) controlling one inline panel containing input JSON + `ToolResult`.

- [ ] **Step 1: Pin existing card-mode tests, then write the failing line-mode tests**

In `web/src/components/session/__tests__/ToolCallCard.test.tsx`, the existing suite asserts the card layout (separate `/args/` and `/result/` buttons). Card mode is no longer the default, so make those tests explicit by adding `density="card"` to every existing `render(<ToolCallCard ... />)` call in the file. For example, change:

```tsx
render(<ToolCallCard block={bashBlock} />)
```
to:
```tsx
render(<ToolCallCard block={bashBlock} density="card" />)
```

Apply that to all existing `render(<ToolCallCard .../>)` calls in the file (including the `defaultResultOpen` test and the final `renders ToolResult ...` test).

Then append a new describe block for line mode at the end of the file (inside the top-level `describe('ToolCallCard', ...)`):

```tsx
describe('line density', () => {
  it('renders the tool name and key arg in a single row', () => {
    render(<ToolCallCard block={bashBlock} density="line" />)
    expect(screen.getByText('Bash')).toBeInTheDocument()
    expect(screen.getByText('npm test')).toBeInTheDocument()
  })

  it('is collapsed by default (detail hidden)', () => {
    render(<ToolCallCard block={bashBlockWithResult} density="line" />)
    const toggle = screen.getByRole('button', { name: /tool details/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    const regionId = toggle.getAttribute('aria-controls')!
    expect(document.getElementById(regionId)!.hidden).toBe(true)
  })

  it('clicking the row reveals input JSON and result', async () => {
    const user = userEvent.setup()
    render(<ToolCallCard block={bashBlockWithResult} density="line" />)
    const toggle = screen.getByRole('button', { name: /tool details/i })
    await user.click(toggle)
    const regionId = toggle.getAttribute('aria-controls')!
    expect(document.getElementById(regionId)!.hidden).toBe(false)
    expect(screen.getByText(/test passed/)).toBeInTheDocument()
  })

  it('shows ✓ and a size hint in the row when result is ok', () => {
    render(<ToolCallCard block={bashBlockWithResult} density="line" />)
    const toggle = screen.getByRole('button', { name: /tool details/i })
    expect(toggle.textContent).toContain('✓')
    expect(toggle.textContent).toMatch(/3\s*lines?/)
  })

  it('shows ✕ when result failed', () => {
    render(<ToolCallCard block={bashBlockWithError} density="line" />)
    const toggle = screen.getByRole('button', { name: /tool details/i })
    expect(toggle.textContent).toContain('✕')
  })

  it('defaultResultOpen opens the detail panel', () => {
    render(<ToolCallCard block={bashBlockWithResult} density="line" defaultResultOpen />)
    const toggle = screen.getByRole('button', { name: /tool details/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
  })
})
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `cd web && npx vitest run src/components/session/__tests__/ToolCallCard.test.tsx`
Expected: the new `line density` tests FAIL (no button named "tool details"); existing `density="card"` tests PASS.

- [ ] **Step 3: Implement line mode**

In `web/src/components/session/ToolCallCard.tsx`, update the Props and add a line-mode branch. Change the `Props` interface and the start of the component:

```tsx
interface Props {
  block: ToolBlock
  /** Compact single-line row ('line') or full bordered card ('card'). Default 'card'. */
  density?: 'line' | 'card'
  /** Auto-expand the result/detail panel (use for the most recent tool call). Default false. */
  defaultResultOpen?: boolean
}
```

```tsx
export function ToolCallCard({ block, density = 'card', defaultResultOpen = false }: Props) {
  const [argsOpen, setArgsOpen] = useState(false)
  const [resultOpen, setResultOpen] = useState(defaultResultOpen)
  const argsRegionId = useId()
  const resultRegionId = useId()
  const detailRegionId = useId()

  const summary = keyArg(block.toolKind, block.input)

  if (density === 'line') {
    const ok = block.result?.ok ?? true
    const hint = block.result ? sizeHint(block.toolKind, block.result.payload) : ''
    return (
      <div className="text-sm">
        <button
          type="button"
          aria-label="Toggle tool details"
          aria-expanded={resultOpen}
          aria-controls={detailRegionId}
          onClick={() => setResultOpen((v) => !v)}
          className={cn(
            'flex w-full items-center gap-2 px-3 py-0.5 text-left',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            'min-h-[44px] hover:bg-gray-50 dark:hover:bg-gray-900/40 rounded',
          )}
        >
          <span className="shrink-0 text-gray-500 dark:text-gray-400">{kindIcon(block.toolKind)}</span>
          <span className="shrink-0 font-medium text-gray-900 dark:text-gray-100">{block.name}</span>
          {summary && (
            <span className="truncate text-xs text-gray-500 dark:text-gray-400">{summary}</span>
          )}
          {block.result && (
            <span className={cn('shrink-0 font-bold', ok ? 'text-green-700 dark:text-green-400' : 'text-red-700 dark:text-red-400')}>
              {ok ? '✓' : '✕'}
            </span>
          )}
          {hint && <span className="ml-auto shrink-0 text-xs opacity-60">{hint}</span>}
          <span className={cn('shrink-0 text-gray-400 dark:text-gray-500', hint ? 'ml-1' : 'ml-auto')}>
            {resultOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </span>
        </button>

        <div
          id={detailRegionId}
          hidden={!resultOpen}
          className="border-l-2 border-gray-200 dark:border-gray-700 ml-4 px-3 py-2"
        >
          <pre className="mb-2 overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs font-mono text-gray-800 dark:text-gray-200">
            {JSON.stringify(block.input, null, 2)}
          </pre>
          {block.result && (
            <ToolResult kind={block.toolKind} ok={block.result.ok} payload={block.result.payload} />
          )}
        </div>
      </div>
    )
  }

  return (
    // ... existing card-mode JSX unchanged (the current `return (<div className="my-1 rounded border ...">`) ...
  )
}
```

Leave the entire existing card-mode `return (...)` block exactly as it is — only move it below the new `if (density === 'line')` branch. Keep the `argsOpen`/`argsRegionId`/`resultRegionId` bindings; they remain used by card mode.

- [ ] **Step 4: Run tests to verify all pass**

Run: `cd web && npx vitest run src/components/session/__tests__/ToolCallCard.test.tsx`
Expected: PASS (all existing card-mode tests + 6 new line-mode tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/ToolCallCard.tsx web/src/components/session/__tests__/ToolCallCard.test.tsx
git commit -m "feat(session): add compact line density to ToolCallCard"
```

---

## Task 3: `ToolGroup` component

**Files:**
- Create: `web/src/components/session/ToolGroup.tsx`
- Test: `web/src/components/session/__tests__/ToolGroup.test.tsx`

**Interfaces:**
- Consumes: `ToolBlock`, `ToolDetailMode` from `@/session/groupBlocks`; `ToolCallCard` (density `'line'`).
- Produces: `ToolGroup` component:
  ```ts
  interface ToolGroupProps {
    tools: ToolBlock[]
    isLiveTail: boolean
    toolDetailMode: ToolDetailMode
    threshold?: number   // default 4
  }
  ```
  Fold rule (auto): summary shown when every tool is complete (`result` defined), the group is not the live tail, and `tools.length >= threshold`. `all-open` forces rows; `all-closed` forces a fold summary. In the live tail, the last tool's card opens its detail by default.

- [ ] **Step 1: Write the failing tests**

Create `web/src/components/session/__tests__/ToolGroup.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ToolGroup } from '../ToolGroup'
import type { ToolBlock } from '@/session/groupBlocks'

function tool(id: string, withResult = true): ToolBlock {
  const base: ToolBlock = { kind: 'tool', id, toolId: id, name: 'Read', toolKind: 'read', input: { file_path: `/f/${id}` } }
  return withResult ? { ...base, result: { ok: true, payload: { content: 'x' } } } : base
}

const fourDone = [tool('a'), tool('b'), tool('c'), tool('d')]

describe('ToolGroup', () => {
  it('folds a completed run of >= threshold tools into a summary (auto, not live)', () => {
    render(<ToolGroup tools={fourDone} isLiveTail={false} toolDetailMode="auto" />)
    const header = screen.getByRole('button', { name: /tool group/i })
    expect(header.textContent).toMatch(/4 tool calls/)
    expect(header).toHaveAttribute('aria-expanded', 'false')
    const regionId = header.getAttribute('aria-controls')!
    expect(document.getElementById(regionId)!.hidden).toBe(true)
  })

  it('clicking the summary expands the rows', async () => {
    const user = userEvent.setup()
    render(<ToolGroup tools={fourDone} isLiveTail={false} toolDetailMode="auto" />)
    const header = screen.getByRole('button', { name: /tool group/i })
    await user.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('does not fold a short run (< threshold) in auto mode', () => {
    render(<ToolGroup tools={[tool('a'), tool('b'), tool('c')]} isLiveTail={false} toolDetailMode="auto" />)
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(3)
  })

  it('does not fold the live tail even when long', () => {
    render(<ToolGroup tools={fourDone} isLiveTail toolDetailMode="auto" />)
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('does not fold when a tool is still incomplete', () => {
    const withPending = [tool('a'), tool('b'), tool('c'), tool('d', false)]
    render(<ToolGroup tools={withPending} isLiveTail={false} toolDetailMode="auto" />)
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
  })

  it('all-closed folds even a short run', () => {
    render(<ToolGroup tools={[tool('a'), tool('b')]} isLiveTail={false} toolDetailMode="all-closed" />)
    const header = screen.getByRole('button', { name: /tool group/i })
    expect(header).toHaveAttribute('aria-expanded', 'false')
  })

  it('all-open shows rows with no summary header', () => {
    render(<ToolGroup tools={fourDone} isLiveTail={false} toolDetailMode="all-open" />)
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('marks the summary with ✕ when any tool failed', () => {
    const failed: ToolBlock = { ...tool('e'), result: { ok: false, payload: { content: 'boom' } } }
    render(<ToolGroup tools={[tool('a'), tool('b'), tool('c'), failed]} isLiveTail={false} toolDetailMode="auto" />)
    const header = screen.getByRole('button', { name: /tool group/i })
    expect(header.textContent).toContain('✕')
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/components/session/__tests__/ToolGroup.test.tsx`
Expected: FAIL — cannot find module `../ToolGroup`.

- [ ] **Step 3: Implement `ToolGroup`**

Create `web/src/components/session/ToolGroup.tsx`:

```tsx
import { useEffect, useId, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ToolBlock, ToolDetailMode } from '@/session/groupBlocks'
import { ToolCallCard } from './ToolCallCard'

interface ToolGroupProps {
  tools: ToolBlock[]
  /** True when this group is the newest activity (last group of the last turn). */
  isLiveTail: boolean
  toolDetailMode: ToolDetailMode
  /** Minimum completed tools before an auto-fold summary appears. Default 4. */
  threshold?: number
}

export function ToolGroup({ tools, isLiveTail, toolDetailMode, threshold = 4 }: ToolGroupProps) {
  const regionId = useId()
  const [userExpanded, setUserExpanded] = useState(false)

  // Reset per-group override whenever the global mode changes so the global
  // expand/collapse-all toggle produces a uniform state.
  useEffect(() => { setUserExpanded(false) }, [toolDetailMode])

  const allComplete = tools.every((t) => t.result !== undefined)
  const autoFold = allComplete && !isLiveTail && tools.length >= threshold

  let folded: boolean
  if (toolDetailMode === 'all-open') folded = false
  else if (toolDetailMode === 'all-closed') folded = !userExpanded
  else folded = autoFold && !userExpanded

  // Show the fold/collapse header whenever folding applies. Never in all-open.
  const showHeader = toolDetailMode !== 'all-open' && (toolDetailMode === 'all-closed' || autoFold)

  const anyFailed = tools.some((t) => t.result && !t.result.ok)
  const glyph = anyFailed ? '✕' : '✓'
  const names = [...new Set(tools.map((t) => t.name))]
  const nameList = names.slice(0, 3).join(', ') + (names.length > 3 ? '…' : '')
  const label = `${tools.length} tool ${tools.length === 1 ? 'call' : 'calls'} — ${nameList}`

  return (
    <div className="my-1">
      {showHeader && (
        <button
          type="button"
          aria-label="Toggle tool group"
          aria-expanded={!folded}
          aria-controls={regionId}
          onClick={() => setUserExpanded((v) => !v)}
          className={cn(
            'flex w-full items-center gap-2 px-3 py-0.5 text-left text-sm',
            'min-h-[44px] rounded hover:bg-gray-50 dark:hover:bg-gray-900/40',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            anyFailed ? 'text-red-700 dark:text-red-400' : 'text-gray-600 dark:text-gray-300',
          )}
        >
          <span className="shrink-0 font-bold">{glyph}</span>
          <span className="truncate">{label}</span>
          <span className="ml-auto shrink-0 text-gray-400 dark:text-gray-500">
            {folded ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          </span>
        </button>
      )}

      <div id={regionId} hidden={folded}>
        {tools.map((t, i) => (
          <ToolCallCard
            key={t.id}
            block={t}
            density="line"
            defaultResultOpen={isLiveTail && i === tools.length - 1}
          />
        ))}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/components/session/__tests__/ToolGroup.test.tsx`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/ToolGroup.tsx web/src/components/session/__tests__/ToolGroup.test.tsx
git commit -m "feat(session): add ToolGroup with fold summary for completed tool runs"
```

---

## Task 4: Wire `Turn` + `TranscriptView` to group tools

**Files:**
- Modify: `web/src/components/session/Turn.tsx`
- Modify: `web/src/components/session/TranscriptView.tsx`
- Test: `web/src/components/session/__tests__/Turn.test.tsx` (create)

**Interfaces:**
- Consumes: `groupBlocks`, `ToolDetailMode` from `@/session/groupBlocks`; `ToolGroup` from `./ToolGroup`.
- Produces:
  - `Turn` Props gain `isLastTurn: boolean` and `toolDetailMode: ToolDetailMode`.
  - `TranscriptView` Props gain `toolDetailMode: ToolDetailMode`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/Turn.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Turn } from '../Turn'
import type { Turn as TurnType, Block } from '@/session/reducer'

function tool(id: string): Block {
  return {
    kind: 'tool', id, toolId: id, name: 'Read', toolKind: 'read',
    input: { file_path: `/f/${id}` }, result: { ok: true, payload: { content: 'x' } },
  }
}

function makeTurn(blocks: Block[]): TurnType {
  return { id: 'turn-1', role: 'assistant', blocks }
}

const noop = () => {}

describe('Turn tool grouping', () => {
  it('folds a long completed tool run when this is NOT the last turn', () => {
    render(
      <Turn
        turn={makeTurn([tool('a'), tool('b'), tool('c'), tool('d')])}
        permissionMode="interactive"
        isLastTurn={false}
        toolDetailMode="auto"
        onDecision={noop}
        onAnswer={noop}
      />,
    )
    expect(screen.getByRole('button', { name: /tool group/i }).textContent).toMatch(/4 tool calls/)
  })

  it('keeps the tool run expanded when this IS the last turn (live tail)', () => {
    render(
      <Turn
        turn={makeTurn([tool('a'), tool('b'), tool('c'), tool('d')])}
        permissionMode="interactive"
        isLastTurn
        toolDetailMode="auto"
        onDecision={noop}
        onAnswer={noop}
      />,
    )
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('renders text blocks between separate tool groups in order', () => {
    const blocks: Block[] = [tool('a'), { kind: 'text', id: 't', text: 'hello world' }, tool('b'), tool('c')]
    render(
      <Turn
        turn={makeTurn(blocks)}
        permissionMode="interactive"
        isLastTurn={false}
        toolDetailMode="auto"
        onDecision={noop}
        onAnswer={noop}
      />,
    )
    expect(screen.getByText('hello world')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/session/__tests__/Turn.test.tsx`
Expected: FAIL — `Turn` does not accept `isLastTurn`/`toolDetailMode`; no "tool group" button.

- [ ] **Step 3: Update `Turn.tsx`**

Replace the contents of `web/src/components/session/Turn.tsx` with:

```tsx
import { memo } from 'react'
import type { Turn as TurnType } from '@/session/reducer'
import { groupBlocks, type ToolDetailMode } from '@/session/groupBlocks'
import { AssistantText } from './AssistantText'
import { UserBubble } from './UserBubble'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolCallCard } from './ToolCallCard'
import { ToolGroup } from './ToolGroup'
import { PermissionCard } from './PermissionCard'
import { QuestionCard } from './QuestionCard'

interface Props {
  turn: TurnType
  permissionMode: string
  isLastTurn: boolean
  toolDetailMode: ToolDetailMode
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
}

export const Turn = memo(function Turn({
  turn, permissionMode, isLastTurn, toolDetailMode, onDecision, onAnswer,
}: Props) {
  const isUser = turn.role === 'user'
  const items = groupBlocks(turn.blocks)
  const lastIdx = items.length - 1

  return (
    <div className="px-4 py-2">
      {items.map((item, idx) => {
        if (item.kind === 'toolGroup') {
          // The live tail is the final render item of the last turn.
          const isLiveTail = isLastTurn && idx === lastIdx
          return (
            <ToolGroup
              key={item.tools[0].id}
              tools={item.tools}
              isLiveTail={isLiveTail}
              toolDetailMode={toolDetailMode}
            />
          )
        }

        const block = item.block
        switch (block.kind) {
          case 'text':
            return isUser
              ? <UserBubble key={block.id} text={block.text} />
              : <AssistantText key={block.id} text={block.text} streaming={false} />
          case 'thinking':
            return <ThinkingBlock key={block.id} text={block.text} />
          case 'tool':
            // Unreachable: tool blocks are coalesced into toolGroup items above.
            return <ToolCallCard key={block.id} block={block} density="line" />
          case 'permission':
            return <PermissionCard key={block.id} block={block} mode={permissionMode} onDecision={onDecision} />
          case 'question':
            return <QuestionCard key={block.id} block={block} onAnswer={onAnswer} />
          default:
            return null
        }
      })}
    </div>
  )
})
```

- [ ] **Step 4: Run the Turn test to verify it passes**

Run: `cd web && npx vitest run src/components/session/__tests__/Turn.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Update `TranscriptView.tsx` to pass the new props**

In `web/src/components/session/TranscriptView.tsx`:

Add the import and prop type:

```tsx
import type { ToolDetailMode } from '@/session/groupBlocks'
```

Add `toolDetailMode` to `Props`:

```tsx
interface Props {
  turns: TurnType[]
  permissionMode: string
  toolDetailMode: ToolDetailMode
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
}
```

Destructure it and pass `isLastTurn` + `toolDetailMode` into each `Turn`:

```tsx
export function TranscriptView({ turns, permissionMode, toolDetailMode, onDecision, onAnswer }: Props) {
  // ...existing refs/handlers unchanged...
  return (
    <div ref={containerRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
      {turns.map((turn, idx) => (
        <Turn
          key={turn.id}
          turn={turn}
          permissionMode={permissionMode}
          isLastTurn={idx === turns.length - 1}
          toolDetailMode={toolDetailMode}
          onDecision={onDecision}
          onAnswer={onAnswer}
        />
      ))}
      <div ref={endRef} className="h-1" aria-hidden="true" />
    </div>
  )
}
```

- [ ] **Step 6: Run the session test suite to catch prop regressions**

Run: `cd web && npx vitest run src/components/session`
Expected: PASS. (SessionView renders `TranscriptView` without `toolDetailMode` until Task 5 — TypeScript build isn't run by vitest, so existing tests still pass; the prop is wired in Task 5. If any test directly renders `TranscriptView`, it must add `toolDetailMode="auto"` — update such tests now.)

- [ ] **Step 7: Commit**

```bash
git add web/src/components/session/Turn.tsx web/src/components/session/TranscriptView.tsx web/src/components/session/__tests__/Turn.test.tsx
git commit -m "feat(session): group tool blocks in Turn and thread toolDetailMode"
```

---

## Task 5: `SessionView` global toggle (button + `c` keybinding) and docs

**Files:**
- Modify: `web/src/components/session/SessionView.tsx`
- Modify: `web/src/components/ShortcutsHelp.tsx`
- Test: `web/src/components/session/__tests__/SessionView.toolDetail.test.tsx` (create)

**Interfaces:**
- Consumes: `ToolDetailMode` from `@/session/groupBlocks`; passes `toolDetailMode` to `TranscriptView`.
- Produces: a top-bar toggle button (`aria-label` "Collapse tool calls" / "Expand tool calls") and a `c` keyboard shortcut, both flipping `toolDetailMode` between `'all-closed'` and `'all-open'`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/SessionView.toolDetail.test.tsx`. Reuse the mock-socket pattern from `SessionView.keyboard.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionView } from '../SessionView'

const stableMockSocket = {
  attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
  sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
  setPermissionMode: vi.fn(), cancelQueued: vi.fn(),
  on: vi.fn(), off: vi.fn(), connectionState: 'connected' as const, socket: null,
}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return { ...actual, useSessionSocket: () => stableMockSocket }
})

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('SessionView tool-detail toggle', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders a collapse-tool-calls button that flips its label when clicked', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    const btn = screen.getByRole('button', { name: /collapse tool calls/i })
    await user.click(btn)
    expect(screen.getByRole('button', { name: /expand tool calls/i })).toBeInTheDocument()
  })

  it('pressing "c" toggles the tool-detail button label', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    expect(screen.getByRole('button', { name: /collapse tool calls/i })).toBeInTheDocument()
    await user.keyboard('c')
    expect(screen.getByRole('button', { name: /expand tool calls/i })).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/session/__tests__/SessionView.toolDetail.test.tsx`
Expected: FAIL — no button named "collapse tool calls".

- [ ] **Step 3: Implement in `SessionView.tsx`**

Add the import:

```tsx
import type { ToolDetailMode } from '@/session/groupBlocks'
```

Add state near the other `useState` calls (after `rotations`):

```tsx
const [toolDetailMode, setToolDetailMode] = useState<ToolDetailMode>('auto')
const toggleToolDetail = useCallback(
  () => setToolDetailMode((m) => (m === 'all-closed' ? 'all-open' : 'all-closed')),
  [],
)
```

In the keydown effect (`onKey`), add a guarded `c` handler as the first branch, and add `toggleToolDetail` to the dependency array:

```tsx
function onKey(e: KeyboardEvent) {
  const ae = document.activeElement as HTMLElement | null
  const typing = !!ae && (ae.tagName === 'TEXTAREA' || ae.tagName === 'INPUT' || ae.isContentEditable)
  if (!typing && (e.key === 'c' || e.key === 'C') && !e.metaKey && !e.ctrlKey && !e.altKey) {
    e.preventDefault()
    toggleToolDetail()
    return
  }
  if (e.key === 'Tab' && e.shiftKey) {
    // ...existing Shift+Tab branch unchanged...
  } else if (e.key === 'Escape') {
    // ...existing Escape branch unchanged...
  }
}
```

Update the effect deps line:

```tsx
}, [handleSetMode, handleBack, hasOpenAction, toggleToolDetail])
```

Add the toggle button to the top bar — insert it just before the `<div className="ml-auto">` that wraps `StatusBar`:

```tsx
<button
  type="button"
  onClick={toggleToolDetail}
  aria-label={toolDetailMode === 'all-closed' ? 'Expand tool calls' : 'Collapse tool calls'}
  className={cn(
    'ml-auto flex items-center gap-1.5 px-3 py-2 rounded-md min-h-[44px]',
    'text-sm font-medium text-gray-200',
    'hover:bg-gray-700 active:bg-gray-600',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
  )}
>
  {toolDetailMode === 'all-closed' ? 'Expand tools' : 'Collapse tools'}
</button>

<div>
  <StatusBar status={state.status} connectionState={connectionState} />
</div>
```

Note: remove the `ml-auto` from the existing `<div className="ml-auto">` wrapping `StatusBar` (the new button now carries `ml-auto`), so it becomes `<div>`.

Pass `toolDetailMode` to `TranscriptView`:

```tsx
<TranscriptView
  turns={state.turns}
  permissionMode={mode}
  toolDetailMode={toolDetailMode}
  onDecision={handleDecision}
  onAnswer={handleAnswer}
/>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/components/session/__tests__/SessionView.toolDetail.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Document the shortcut in `ShortcutsHelp.tsx`**

In `web/src/components/ShortcutsHelp.tsx`, add a binding to `IN_SESSION_BINDINGS`:

```tsx
const IN_SESSION_BINDINGS: Binding[] = [
  { keys: 'Esc', description: 'Back to session list' },
  { keys: 'Shift+Tab', description: 'Cycle permission mode' },
  { keys: 'c', description: 'Collapse / expand tool calls' },
]
```

- [ ] **Step 6: Run the full web test suite**

Run: `cd web && npm test`
Expected: PASS (all suites, including new `groupBlocks`, `ToolGroup`, `Turn`, line-mode, and toggle tests).

- [ ] **Step 7: Typecheck and lint**

Run: `cd web && npx tsc --noEmit && npm run lint`
Expected: no errors. (If `SessionView.keyboard.test.tsx` or `SessionView.test.tsx` render `TranscriptView`/`Turn` directly and now fail typecheck for the missing `toolDetailMode`/`isLastTurn` prop, add `toolDetailMode="auto"` / `isLastTurn={false}` to those render calls.)

- [ ] **Step 8: Commit**

```bash
git add web/src/components/session/SessionView.tsx web/src/components/ShortcutsHelp.tsx web/src/components/session/__tests__/SessionView.toolDetail.test.tsx
git commit -m "feat(session): global collapse/expand-all toggle for tool calls (button + c key)"
```

---

## Self-Review

**Spec coverage:**
- Compact-by-default tool rows → Task 2 (line density) + Task 4 (all tools route through ToolGroup → line mode). ✓
- Group fold for completed runs (threshold 4, live tail expanded, most-recent auto-open) → Task 3 (`ToolGroup`) + Task 4 (`isLiveTail` wiring). ✓
- Global expand/collapse-all (button + keybinding + ShortcutsHelp) → Task 5. ✓
- `groupBlocks` pure helper + unit tests → Task 1. ✓
- Ephemeral state (no localStorage) → all collapse state is `useState`; nothing persisted. ✓
- No backend/protocol change → only `web/src/` render files touched. ✓
- Non-goal (xterm view untouched, non-tool blocks unchanged) → Turn still renders text/thinking/permission/question as before. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. The card-mode `return (...)` in Task 2 references the existing block verbatim rather than re-pasting it — acceptable because the instruction is "leave unchanged, move below the new branch," not "write new code." ✓

**Type consistency:** `ToolDetailMode` and `ToolBlock` defined once in `groupBlocks.ts` and imported everywhere. `groupBlocks(): RenderItem[]` matches the `item.kind === 'toolGroup'` / `item.block` usage in Turn. `ToolGroupProps` (`tools`, `isLiveTail`, `toolDetailMode`, `threshold?`) matches Turn's usage and the tests. `ToolCallCard` `density`/`defaultResultOpen` props match all call sites. ✓
