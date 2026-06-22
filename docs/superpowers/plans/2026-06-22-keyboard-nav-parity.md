# Keyboard Navigation Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sessions-list ↑/↓ navigation reliable and add ↑/↓ command-history recall inside a session, moving the ccswitch web UI toward keyboard parity with Claude Code.

**Architecture:** Part A hardens existing list-nav logic by extracting the Dashboard's global keydown listener into a focused, unit-testable hook and adding regression tests. Part B adds prompt-history recall: a pure helper derives history from the transcript turns the backend already replays, and the Composer gains ↑/↓ recall with draft preservation, reusing the existing `caretLineEdges` boundary helper.

**Tech Stack:** React + TypeScript + Vite; Vitest + @testing-library/react + @testing-library/user-event; Tailwind.

## Global Constraints

- Branch from `origin/master`: `feat/keyboard-nav-parity` (already created).
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit (constitution v1.1.0 Principle V).
- Single source of truth / no dead code (Principle VI): reuse `web/src/lib/caretLineEdges.ts`; do not add a second history store — derive from existing `reducer` turns.
- Verification (run in `web/`): `npx vitest run`, `npx tsc --noEmit`, `npx eslint .`. All must be green before each commit and before the PR.
- Fresh-worktree env caveat: this worktree may have no built env. Symlink `web/node_modules` from the main checkout and run `npx vitest`/`tsc`/`eslint` from there; run server tests (none needed here) against the main checkout's prebuilt `.venv` if required.
- Accessibility: the sessions list uses virtual focus (`aria-activedescendant`); do not move real DOM focus to list rows. Min touch target 44px is already satisfied by existing styles — don't regress it.

---

## Task 0: Environment baseline (no code)

**Files:** none (setup only).

- [ ] **Step 1: Ensure web deps are available**

```bash
cd web
# If node_modules is missing in this worktree, symlink from the main checkout:
# ln -s /home/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/eee6dcb1/web/node_modules web/node_modules  # adjust source to main checkout
ls node_modules/.bin/vitest >/dev/null && echo "vitest present"
```

- [ ] **Step 2: Establish a green baseline**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`
Expected: all pass on the untouched `origin/master` branch. If anything fails pre-change, stop and report — do not build on a red baseline.

---

## Task 1: Reproduce & diagnose sessions-list ↑/↓ (Part A)

**Goal:** Determine whether issue 1 is a live defect or deploy lag, so later tasks either just add regression coverage or also fix a bug.

**Files:**
- Read-only investigation: `web/src/components/Dashboard.tsx` (global listener ~lines 1218–1238, `handleNavigate` ~1202), `web/src/components/DispatchBar.tsx` (`handleKeyDown`), `web/src/lib/caretLineEdges.ts`.

- [ ] **Step 1: Trace the two nav paths**

Confirm by reading the code:
1. DispatchBar textarea: ↑ navigates when `empty || caretLineEdges(text, caret).atFirstLine`; ↓ when `empty || atLastLine`.
2. Dashboard global `document` keydown: navigates when `activeTab === 'sessions'`, `e.target` is not `INPUT/TEXTAREA/SELECT`/contentEditable, and no `role="dialog"` is open.

- [ ] **Step 2: Identify any gap**

Write down the exact condition (if any) where neither path fires for an empty/single-line prompt while on the Sessions tab. Hypothesis to confirm or refute: there is **no** live logic gap and the report was deploy lag (auto-deploy from master landed only in commit `6ecfb36`). 

- [ ] **Step 3: Record the verdict in the PR description draft**

If a genuine defect is found, note the precise failing condition; the fix is then folded into Task 2 or Task 3 as an extra failing test + minimal change. If none is found, Tasks 2–3 add regression coverage only. No commit in this task.

---

## Task 2: Regression test — single-line draft navigates the list (Part A)

**Files:**
- Test: `web/src/components/__tests__/DispatchBar.test.tsx` (extend existing file)

**Interfaces:**
- Consumes: `DispatchBar` props incl. `onNavigate: (dir: 'up'|'down') => void` and the `makeProps` helper already defined at the top of the test file.

- [ ] **Step 1: Add the failing/missing test**

Append inside the existing `describe('DispatchBar', …)` block:

```tsx
it('ArrowUp/ArrowDown with single-line text navigate the list (no caret destination)', async () => {
  const user = userEvent.setup()
  const onNavigate = vi.fn()
  render(<DispatchBar {...makeProps({ onNavigate })} />)
  const box = screen.getByRole('textbox', { name: /new session prompt/i })
  await user.type(box, 'fix the bug')        // single line, caret at end
  await user.keyboard('{ArrowUp}')
  expect(onNavigate).toHaveBeenCalledWith('up')
  await user.keyboard('{ArrowDown}')
  expect(onNavigate).toHaveBeenCalledWith('down')
})
```

- [ ] **Step 2: Run it**

Run (in `web/`): `npx vitest run src/components/__tests__/DispatchBar.test.tsx -t "single-line text navigate"`
Expected: PASS if the existing logic is correct (single-line ⇒ `atFirstLine && atLastLine` are both true). If it FAILS, a real defect exists — fix `DispatchBar.handleKeyDown` minimally so single-line drafts navigate, then re-run to green.

- [ ] **Step 3: Commit**

```bash
git add web/src/components/__tests__/DispatchBar.test.tsx web/src/components/DispatchBar.tsx
git commit -m "test(dispatch): lock single-line ↑/↓ list navigation"
```

---

## Task 3: Extract & unit-test the global list-nav hook (Part A)

**Goal:** Make the Dashboard global ↑/↓ listener independently testable and keep the 1600-line `Dashboard.tsx` from growing. Behavior is unchanged — this is a refactor that adds the missing regression coverage.

**Files:**
- Create: `web/src/components/useGlobalListNav.ts`
- Create: `web/src/components/__tests__/useGlobalListNav.test.tsx`
- Modify: `web/src/components/Dashboard.tsx` (replace the inline `useEffect` global listener with a call to the hook)

**Interfaces:**
- Produces: `useGlobalListNav({ enabled: boolean; onNavigate: (dir: 'up' | 'down') => void }): void`
  - Subscribes a `document` keydown listener. On `ArrowUp`/`ArrowDown`, when `enabled` is true, `e.defaultPrevented` is false, `e.target` is not `INPUT`/`TEXTAREA`/`SELECT`/contentEditable, and no `[role="dialog"]` is in the document, it calls `e.preventDefault()` then `onNavigate('up' | 'down')`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/useGlobalListNav.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useGlobalListNav } from '../useGlobalListNav'

function Harness(props: { enabled: boolean; onNavigate: (d: 'up' | 'down') => void; withDialog?: boolean; withInput?: boolean }) {
  useGlobalListNav({ enabled: props.enabled, onNavigate: props.onNavigate })
  return (
    <div>
      <button>focusable</button>
      {props.withInput && <input aria-label="field" />}
      {props.withDialog && <div role="dialog">modal</div>}
    </div>
  )
}

describe('useGlobalListNav', () => {
  it('navigates up/down when enabled and focus is on a non-input element', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<Harness enabled onNavigate={onNavigate} />)
    await user.click(screen.getByRole('button', { name: /focusable/i }))
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).toHaveBeenCalledWith('down')
    await user.keyboard('{ArrowUp}')
    expect(onNavigate).toHaveBeenCalledWith('up')
  })

  it('does nothing when disabled', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<Harness enabled={false} onNavigate={onNavigate} />)
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).not.toHaveBeenCalled()
  })

  it('bails when focus is inside an input', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<Harness enabled withInput onNavigate={onNavigate} />)
    await user.click(screen.getByLabelText('field'))
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).not.toHaveBeenCalled()
  })

  it('bails when a dialog is open', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<Harness enabled withDialog onNavigate={onNavigate} />)
    await user.click(screen.getByRole('button', { name: /focusable/i }))
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run it (red)**

Run (in `web/`): `npx vitest run src/components/__tests__/useGlobalListNav.test.tsx`
Expected: FAIL — `useGlobalListNav` does not exist yet.

- [ ] **Step 3: Implement the hook**

Create `web/src/components/useGlobalListNav.ts`:

```ts
import { useEffect } from 'react'

interface Options {
  /** When false, the listener is inert (e.g. not on the Sessions tab). */
  enabled: boolean
  onNavigate: (dir: 'up' | 'down') => void
}

/**
 * Up/Down arrows navigate the session list even when real focus is NOT in the
 * dispatch textarea — e.g. focus is parked on the "Sessions" tab button. Bails
 * whenever focus is inside an editable/form control or an open dialog, so it
 * never double-handles a key the textarea/select already consumed.
 */
export function useGlobalListNav({ enabled, onNavigate }: Options): void {
  useEffect(() => {
    if (!enabled) return
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
      if (e.defaultPrevented) return
      const t = e.target as HTMLElement | null
      const tag = t?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable) return
      if (document.querySelector('[role="dialog"]')) return
      e.preventDefault()
      onNavigate(e.key === 'ArrowUp' ? 'up' : 'down')
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [enabled, onNavigate])
}
```

- [ ] **Step 4: Run it (green)**

Run (in `web/`): `npx vitest run src/components/__tests__/useGlobalListNav.test.tsx`
Expected: PASS (all 4).

- [ ] **Step 5: Swap Dashboard to use the hook**

In `web/src/components/Dashboard.tsx`: delete the inline `useEffect` that registers the `onKeyDown` global ArrowUp/Down listener (the block whose comment begins "Up/Down arrows navigate the session list even when real focus is NOT…"). Add an import and a single call near the other `handleNavigate` wiring:

```tsx
import { useGlobalListNav } from './useGlobalListNav'
// …inside the component, after handleNavigate is defined:
useGlobalListNav({ enabled: activeTab === 'sessions', onNavigate: handleNavigate })
```

Wrap `handleNavigate` in `useCallback(…, [sessions.length])` if ESLint's exhaustive-deps flags the new hook dependency; the functional `setHighlightedIndex` updater keeps it correct across renders.

- [ ] **Step 6: Verify nothing regressed**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`
Expected: all green (existing Dashboard tab/settings tests still pass).

- [ ] **Step 7: Commit**

```bash
git add web/src/components/useGlobalListNav.ts web/src/components/__tests__/useGlobalListNav.test.tsx web/src/components/Dashboard.tsx
git commit -m "refactor(dashboard): extract useGlobalListNav hook + regression tests for list ↑/↓"
```

---

## Task 4: `deriveHistory` helper (Part B)

**Goal:** Turn the transcript's user turns into an ordered prompt-history list.

**Files:**
- Create: `web/src/session/history.ts`
- Create: `web/src/session/__tests__/history.test.ts`

**Interfaces:**
- Consumes: `Turn` and `OPTIMISTIC_USER_ID` from `web/src/session/reducer.ts`. `Turn = { id: string; role: 'user' | 'assistant'; blocks: Block[] }`; a user turn's text lives in a block with `kind: 'text'` and a `text: string` field.
- Produces: `deriveHistory(turns: Turn[]): string[]` — previously-sent prompt texts, **oldest → newest**, excluding the optimistic seed turn and any empty-text turns.

- [ ] **Step 1: Write the failing test**

Create `web/src/session/__tests__/history.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { deriveHistory } from '../history'
import { OPTIMISTIC_USER_ID, type Turn } from '../reducer'

const userTurn = (id: string, text: string): Turn => ({
  id, role: 'user', blocks: [{ kind: 'text', id: `${id}:u`, text }],
})
const asstTurn = (id: string, text: string): Turn => ({
  id, role: 'assistant', blocks: [{ kind: 'text', id: `${id}:a`, text }],
})

describe('deriveHistory', () => {
  it('returns user prompts oldest→newest, ignoring assistant turns', () => {
    const turns = [userTurn('t1', 'first'), asstTurn('t1a', 'reply'), userTurn('t2', 'second')]
    expect(deriveHistory(turns)).toEqual(['first', 'second'])
  })

  it('excludes the optimistic seed turn', () => {
    const turns = [userTurn('t1', 'first'), { ...userTurn(OPTIMISTIC_USER_ID, 'in flight') }]
    expect(deriveHistory(turns)).toEqual(['first'])
  })

  it('skips empty-text user turns and keeps legitimate duplicates', () => {
    const turns = [userTurn('t1', 'dup'), userTurn('t2', ''), userTurn('t3', 'dup')]
    expect(deriveHistory(turns)).toEqual(['dup', 'dup'])
  })

  it('returns [] for no user turns', () => {
    expect(deriveHistory([asstTurn('a', 'x')])).toEqual([])
  })
})
```

- [ ] **Step 2: Run it (red)**

Run (in `web/`): `npx vitest run src/session/__tests__/history.test.ts`
Expected: FAIL — `deriveHistory` not defined.

- [ ] **Step 3: Implement**

Create `web/src/session/history.ts`:

```ts
import { OPTIMISTIC_USER_ID, type Turn } from './reducer'

/**
 * Previously-sent prompt texts for a session, oldest → newest. Derived from the
 * transcript the backend replays on attach (so history is persisted "for free"),
 * excluding the transient optimistic-seed turn and any empty-text turns.
 */
export function deriveHistory(turns: Turn[]): string[] {
  const out: string[] = []
  for (const t of turns) {
    if (t.role !== 'user' || t.id === OPTIMISTIC_USER_ID) continue
    for (const b of t.blocks) {
      if (b.kind === 'text') {
        if (b.text) out.push(b.text)
        break
      }
    }
  }
  return out
}
```

If `Turn` is not exported from `reducer.ts`, add `export` to its `interface Turn` declaration (it is already exported per current source; verify before editing).

- [ ] **Step 4: Run it (green)**

Run (in `web/`): `npx vitest run src/session/__tests__/history.test.ts`
Expected: PASS (all 4).

- [ ] **Step 5: Commit**

```bash
git add web/src/session/history.ts web/src/session/__tests__/history.test.ts
git commit -m "feat(session): deriveHistory — prompt history from transcript turns"
```

---

## Task 5: Composer ↑/↓ history recall (Part B)

**Goal:** ↑/↓ in the message box recall previous prompts with draft preservation, yielding to the slash-command palette and the line-boundary rule.

**Files:**
- Modify: `web/src/components/session/Composer.tsx`
- Test: `web/src/components/session/__tests__/Composer.test.tsx` (extend)

**Interfaces:**
- Consumes: `deriveHistory` output via a new optional prop `history?: string[]` (oldest → newest). Reuses `caretLineEdges` from `web/src/lib/caretLineEdges.ts`.
- Produces: no new exports; behavior only.

- [ ] **Step 1: Write the failing tests**

Append inside `describe('Composer', …)` in `web/src/components/session/__tests__/Composer.test.tsx`:

```tsx
it('ArrowUp on empty input recalls the most recent prompt, then older', async () => {
  const user = userEvent.setup()
  render(<Composer {...defaults} history={['older', 'newer']} />)
  const box = screen.getByRole('textbox', { name: /message/i })
  box.focus()
  await user.keyboard('{ArrowUp}')
  expect(box).toHaveValue('newer')
  await user.keyboard('{ArrowUp}')
  expect(box).toHaveValue('older')
  await user.keyboard('{ArrowUp}') // clamped at oldest
  expect(box).toHaveValue('older')
})

it('ArrowDown walks back toward the live draft and restores it', async () => {
  const user = userEvent.setup()
  render(<Composer {...defaults} history={['older', 'newer']} />)
  const box = screen.getByRole('textbox', { name: /message/i })
  await user.type(box, 'draft in progress')
  await user.keyboard('{ArrowUp}')          // stashes draft, shows newest
  expect(box).toHaveValue('newer')
  await user.keyboard('{ArrowDown}')         // back to draft
  expect(box).toHaveValue('draft in progress')
})

it('typing after a recall detaches history (next ArrowUp starts from newest)', async () => {
  const user = userEvent.setup()
  render(<Composer {...defaults} history={['older', 'newer']} />)
  const box = screen.getByRole('textbox', { name: /message/i })
  box.focus()
  await user.keyboard('{ArrowUp}{ArrowUp}')  // now showing 'older'
  await user.type(box, 'X')                  // edit detaches
  await user.keyboard('{ArrowUp}')
  expect(box).toHaveValue('newer')
})

it('does not recall history while the slash-command palette is open', async () => {
  const user = userEvent.setup()
  const goalCmd = { name: 'goal', description: 'set goal', argumentHint: '' }
  render(<Composer {...defaults} history={['older', 'newer']} commands={[goalCmd]} onCommand={vi.fn()} />)
  const box = screen.getByRole('textbox', { name: /message/i })
  await user.type(box, '/g')                 // palette is picking a command
  await user.keyboard('{ArrowUp}')
  expect(box).toHaveValue('/g')              // palette consumed ↑, no history recall
})

it('ArrowUp does nothing when there is no history', async () => {
  const user = userEvent.setup()
  render(<Composer {...defaults} history={[]} />)
  const box = screen.getByRole('textbox', { name: /message/i })
  box.focus()
  await user.keyboard('{ArrowUp}')
  expect(box).toHaveValue('')
})
```

- [ ] **Step 2: Run them (red)**

Run (in `web/`): `npx vitest run src/components/session/__tests__/Composer.test.tsx -t "recall|history|draft"`
Expected: FAIL — `history` prop unused; arrows don't recall.

- [ ] **Step 3: Implement recall in Composer**

In `web/src/components/session/Composer.tsx`:

a) Add the import and prop:

```tsx
import { caretLineEdges } from '@/lib/caretLineEdges'
```

Add `history?: string[]` to `interface Props` and to the destructured params (default `history = []`).

b) Add recall state + effects (near the other hooks):

```tsx
// History recall: histPos 0 = live draft; N = N-th entry counting back from newest.
const [histPos, setHistPos] = useState(0)
const draftRef = useRef('')
const caretToEndRef = useRef(false)

// Reset recall when switching sessions.
useEffect(() => { setHistPos(0); draftRef.current = '' }, [autoFocusKey])

// After a programmatic recall, grow the box and place the caret at the end.
useEffect(() => {
  if (!caretToEndRef.current) return
  caretToEndRef.current = false
  const el = textareaRef.current
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 192)}px`
  const end = el.value.length
  el.setSelectionRange(end, end)
}, [text])

const recallTo = useCallback((pos: number, value: string) => {
  caretToEndRef.current = true
  setHistPos(pos)
  setText(value)
}, [])
```

c) In `handleChange`, detach from history on a real edit (add as the first lines):

```tsx
function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
  if (histPos !== 0) setHistPos(0)
  setText(e.target.value)
  // …existing auto-grow code…
}
```

d) In `handleKeyDown`, add the recall branch **after** the palette check and **before** the Enter check:

```tsx
function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
  if (pickingCommand && paletteRef.current?.handleKeyDown(e)) return

  if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && history.length > 0) {
    const el = e.currentTarget
    const caret = el.selectionStart ?? 0
    const empty = text === ''
    const { atFirstLine, atLastLine } = caretLineEdges(text, caret)
    if (e.key === 'ArrowUp' && (empty || atFirstLine) && histPos < history.length) {
      if (histPos === 0) draftRef.current = text
      e.preventDefault()
      recallTo(histPos + 1, history[history.length - (histPos + 1)])
      return
    }
    if (e.key === 'ArrowDown' && (empty || atLastLine) && histPos > 0) {
      const pos = histPos - 1
      e.preventDefault()
      recallTo(pos, pos === 0 ? draftRef.current : history[history.length - pos])
      return
    }
  }

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submit()
  }
}
```

e) In `submit`, reset recall after clearing (add after `setText('')`):

```tsx
setHistPos(0)
draftRef.current = ''
```

- [ ] **Step 4: Run them (green)**

Run (in `web/`): `npx vitest run src/components/session/__tests__/Composer.test.tsx`
Expected: PASS — new recall tests plus all pre-existing Composer tests (Enter submit, palette, queue, etc.) still green.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/Composer.tsx web/src/components/session/__tests__/Composer.test.tsx
git commit -m "feat(composer): ↑/↓ command-history recall with draft preservation"
```

---

## Task 6: Wire history into both Composer render sites (Part B)

**Goal:** Feed real per-session history to the Composer in both the full-screen and desktop layouts.

**Files:**
- Modify: `web/src/components/session/SessionView.tsx` (Composer at ~line 131)
- Modify: `web/src/components/DesktopShell.tsx` (Composer at ~line 78)

**Interfaces:**
- Consumes: `deriveHistory(rt.state.turns)` and `rt.state.turns: Turn[]`.

- [ ] **Step 1: Wire SessionView**

In `web/src/components/session/SessionView.tsx`, add imports and a memoized value, then pass the prop:

```tsx
import { useMemo } from 'react'
import { deriveHistory } from '@/session/history'
// …inside SessionView, after `const rt = useSessionRuntime(...)`:
const history = useMemo(() => deriveHistory(rt.state.turns), [rt.state.turns])
// …in the <Composer …> element, add:
history={history}
```

- [ ] **Step 2: Wire DesktopShell**

In `web/src/components/DesktopShell.tsx`, mirror the same: import `useMemo` (if not already) and `deriveHistory`, compute `const history = useMemo(() => deriveHistory(rt.state.turns), [rt.state.turns])` adjacent to that Composer, and add `history={history}` to its props. Confirm the local runtime variable name (`rt` or similar) by reading the file; use whatever holds `.state.turns`.

- [ ] **Step 3: Verify**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`
Expected: all green. (No new test file here — behavior is covered by Task 4 + Task 5; this is wiring verified by type-check and the existing SessionView/DesktopShell render tests, if any.)

- [ ] **Step 4: Commit**

```bash
git add web/src/components/session/SessionView.tsx web/src/components/DesktopShell.tsx
git commit -m "feat(session): supply real prompt history to the composer (mobile + desktop)"
```

---

## Task 7: Full verification & PR

**Files:** none (integration).

- [ ] **Step 1: Full green gate**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`
Expected: all pass.

- [ ] **Step 2: Manual smoke (optional but recommended)**

Use the `/run` project skill (or the chrome-devtools MCP against a local dev server) to confirm: on the dashboard, ↑/↓ move the session highlight with an empty and a single-line prompt; inside a session, ↑/↓ recall previous prompts and restore the draft. Capture the verdict from Task 1 (live bug vs deploy lag) in the PR body.

- [ ] **Step 3: Push & open PR**

```bash
git push -u origin feat/keyboard-nav-parity
gh pr create --fill --base master
```

PR body must end with the Claude Code trailer. Per the completion workflow, merge once mergeable: `gh pr merge --merge --delete-branch`.

---

## Follow-up (not in this PR): Part C — keyboard-parity audit

After this PR merges, produce `docs/superpowers/specs/2026-06-22-keyboard-parity-audit.md`: a systematic comparison of Claude Code's keybindings vs the ccswitch web UI, each marked present / partial / absent with a per-gap recommendation, delivered as a prioritized list for the user to choose from. Tracked separately per the approved design.

## Self-Review (completed)

- **Spec coverage:** Part A → Tasks 1–3; Part B → Tasks 4–6; verification/PR → Task 7; Part C → explicitly deferred. ✓
- **Placeholder scan:** every code step contains complete code; no TBD/TODO. ✓
- **Type consistency:** `deriveHistory(turns: Turn[]): string[]` defined in Task 4 and consumed identically in Tasks 5–6; `useGlobalListNav({ enabled, onNavigate })` defined and consumed consistently in Task 3; `history?: string[]` prop consistent between Composer (Task 5) and its callers (Task 6). ✓
- **Boundary reuse:** `caretLineEdges` reused in Composer rather than reimplemented (Principle VI). ✓
