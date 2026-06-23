# Session-list Shortcuts (List-focus Mode) + Global Help Slide-out — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a list-focus mode where the highlighted session row takes real DOM focus and single keys (`c`/`r`/`a`/`d`, `Backspace`/`Delete`, arrows) act on it without colliding with browser or editing natives, driven by one shortcuts registry; and replace the shortcuts modal with a global help slide-out.

**Architecture:** A registry module (`web/src/lib/shortcuts.ts`) is the single source of truth. `SessionCard` (the shared atom rendered by both Dashboard and DesktopShell) matches list-mode keys against it; `SessionList` gives the highlighted card real focus when in list mode. `DispatchBar` enters list mode on `Tab` (empty prompt). The old `Ctrl+R`/`Ctrl+X` branches, the press-twice archive guard, and the `ShortcutsHelp` modal are deleted in the same change.

**Tech Stack:** React 18 + TypeScript + Vite, Tailwind, `lucide-react`, Vitest + Testing Library + `@testing-library/user-event`. No new dependencies.

## Global Constraints

- **Run web commands from `web/`.** Tests: `npx vitest run <file>`. Type gate: `npx tsc -b` (NOT `tsc --noEmit`). Lint: `npx eslint <files>` (CI uses `--max-warnings 0` + `react-hooks` rule).
- **Use the deploy-checkout `node_modules`** if the worktree's is incomplete (symlink it; run the local `.bin`).
- **CI runs the FULL vitest suite** — keep shared prop/type changes consistent across all call sites.
- **Match existing patterns:** custom fixed-overlay dialogs (`role="dialog"`, `aria-modal`, Esc-to-close, focus trap); no shadcn dependency.
- **List-mode keys are unmodified** (`e.key`, no Ctrl/Alt/Meta) — matchers must bail if any of those modifiers is held, so they never hijack `Ctrl+A`/`Ctrl+C` etc.
- Final behavior (verbatim):
  - **Compose mode** (focus in prompt) unchanged, plus `Tab` on an **empty** prompt enters list mode.
  - **List mode** (highlighted row focused): `↑/↓` move · `Enter`/`→`/`Space` open · `c` continue · `r` rename · `a`/`Backspace` archive · `d`/`Delete` delete (confirm dialog) · `Esc`/`←`/`Tab` exit to compose.
  - `Ctrl+R`, `Ctrl+X`, and the press-twice archive guard are **removed**. Archive is single-press in list mode.

---

### Task 1: Shortcuts registry (single source of truth)

**Files:**
- Create: `web/src/lib/shortcuts.ts`
- Test: `web/src/lib/shortcuts.test.ts`

**Interfaces:**
- Produces:
  - `type ListRowAction = { type: 'open' } | { type: 'continue' } | { type: 'rename' } | { type: 'archive' } | { type: 'permanent-delete' } | { type: 'navigate'; dir: 'up' | 'down' } | { type: 'exit' }`
  - `type KeyboardLike = Pick<KeyboardEvent, 'key' | 'code' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'>`
  - `type ShortcutGroup = 'compose' | 'list-mode' | 'in-session'`
  - `interface Shortcut { id; keys; description; group; action?: ListRowAction; match?: (e: KeyboardLike) => boolean }`
  - `const SHORTCUTS: Shortcut[]`
  - `function matchListShortcut(e: KeyboardLike): ListRowAction | undefined`
  - `function shortcutsByGroup(group: ShortcutGroup): Shortcut[]`

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/shortcuts.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { SHORTCUTS, matchListShortcut, shortcutsByGroup, type KeyboardLike } from './shortcuts'

function ev(over: Partial<KeyboardLike>): KeyboardLike {
  return { key: '', code: '', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...over }
}

describe('shortcuts registry — list mode', () => {
  it('maps each unmodified key to the right action', () => {
    expect(matchListShortcut(ev({ key: 'c' }))).toEqual({ type: 'continue' })
    expect(matchListShortcut(ev({ key: 'r' }))).toEqual({ type: 'rename' })
    expect(matchListShortcut(ev({ key: 'a' }))).toEqual({ type: 'archive' })
    expect(matchListShortcut(ev({ key: 'Backspace' }))).toEqual({ type: 'archive' })
    expect(matchListShortcut(ev({ key: 'd' }))).toEqual({ type: 'permanent-delete' })
    expect(matchListShortcut(ev({ key: 'Delete' }))).toEqual({ type: 'permanent-delete' })
    expect(matchListShortcut(ev({ key: 'Enter' }))).toEqual({ type: 'open' })
    expect(matchListShortcut(ev({ key: ' ' }))).toEqual({ type: 'open' })
    expect(matchListShortcut(ev({ key: 'ArrowRight' }))).toEqual({ type: 'open' })
    expect(matchListShortcut(ev({ key: 'ArrowUp' }))).toEqual({ type: 'navigate', dir: 'up' })
    expect(matchListShortcut(ev({ key: 'ArrowDown' }))).toEqual({ type: 'navigate', dir: 'down' })
    expect(matchListShortcut(ev({ key: 'Escape' }))).toEqual({ type: 'exit' })
    expect(matchListShortcut(ev({ key: 'ArrowLeft' }))).toEqual({ type: 'exit' })
    expect(matchListShortcut(ev({ key: 'Tab' }))).toEqual({ type: 'exit' })
  })

  it('does NOT match when a Ctrl/Alt/Meta modifier is held', () => {
    expect(matchListShortcut(ev({ key: 'a', ctrlKey: true }))).toBeUndefined() // Ctrl+A select-all
    expect(matchListShortcut(ev({ key: 'c', metaKey: true }))).toBeUndefined() // Cmd+C copy
    expect(matchListShortcut(ev({ key: 'd', altKey: true }))).toBeUndefined()
  })

  it('exposes display entries for compose and in-session groups', () => {
    expect(shortcutsByGroup('compose').length).toBeGreaterThan(0)
    expect(shortcutsByGroup('in-session').length).toBeGreaterThan(0)
    expect(shortcutsByGroup('list-mode').length).toBeGreaterThan(0)
    for (const s of SHORTCUTS.filter((x) => x.match)) expect(s.keys).toMatch(/\S/)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/lib/shortcuts.test.ts`
Expected: FAIL — `Cannot find module './shortcuts'`.

- [ ] **Step 3: Write the registry**

Create `web/src/lib/shortcuts.ts`:

```ts
/** An action performed against the focused session row in list mode. */
export type ListRowAction =
  | { type: 'open' }
  | { type: 'continue' }
  | { type: 'rename' }
  | { type: 'archive' }
  | { type: 'permanent-delete' }
  | { type: 'navigate'; dir: 'up' | 'down' }
  | { type: 'exit' } // back to compose (focus the prompt)

export type KeyboardLike = Pick<
  KeyboardEvent,
  'key' | 'code' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'
>

export type ShortcutGroup = 'compose' | 'list-mode' | 'in-session'

export interface Shortcut {
  id: string
  keys: string // display string for the help panel
  description: string
  group: ShortcutGroup
  /** Present ⇒ wired into the SessionCard list-mode handler. */
  action?: ListRowAction
  match?: (e: KeyboardLike) => boolean
}

/** Unmodified key predicate — bails if Ctrl/Alt/Meta is held (Shift allowed). */
function plain(pred: (e: KeyboardLike) => boolean) {
  return (e: KeyboardLike): boolean => !e.ctrlKey && !e.altKey && !e.metaKey && pred(e)
}

export const SHORTCUTS: Shortcut[] = [
  // ── List mode — WIRED ────────────────────────────────────────────────
  { id: 'list.open', keys: 'Enter / → / Space', description: 'Open the focused session',
    group: 'list-mode', action: { type: 'open' },
    match: plain((e) => e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowRight') },
  { id: 'list.continue', keys: 'c', description: 'Continue (resume) the focused session',
    group: 'list-mode', action: { type: 'continue' }, match: plain((e) => e.key === 'c' || e.key === 'C') },
  { id: 'list.rename', keys: 'r', description: 'Rename the focused session',
    group: 'list-mode', action: { type: 'rename' }, match: plain((e) => e.key === 'r' || e.key === 'R') },
  { id: 'list.archive', keys: 'a / Backspace', description: 'Archive the focused session',
    group: 'list-mode', action: { type: 'archive' },
    match: plain((e) => e.key === 'a' || e.key === 'A' || e.key === 'Backspace') },
  { id: 'list.delete', keys: 'd / Delete', description: 'Delete the focused session (confirm)',
    group: 'list-mode', action: { type: 'permanent-delete' },
    match: plain((e) => e.key === 'd' || e.key === 'D' || e.key === 'Delete') },
  { id: 'list.up', keys: '↑', description: 'Move to the previous row',
    group: 'list-mode', action: { type: 'navigate', dir: 'up' }, match: plain((e) => e.key === 'ArrowUp') },
  { id: 'list.down', keys: '↓', description: 'Move to the next row',
    group: 'list-mode', action: { type: 'navigate', dir: 'down' }, match: plain((e) => e.key === 'ArrowDown') },
  { id: 'list.exit', keys: 'Esc / ← / Tab', description: 'Return to the prompt (compose)',
    group: 'list-mode', action: { type: 'exit' },
    match: plain((e) => e.key === 'Escape' || e.key === 'ArrowLeft' || e.key === 'Tab') },
  // ── Compose mode — DISPLAY-ONLY (handled in DispatchBar) ──────────────
  { id: 'compose.enterList', keys: 'Tab', description: 'Enter list mode (empty prompt)', group: 'compose' },
  { id: 'compose.nav', keys: '↑ / ↓', description: 'Move the highlight', group: 'compose' },
  { id: 'compose.create', keys: 'Enter', description: 'Create a session from the prompt', group: 'compose' },
  { id: 'compose.open', keys: '→ / Enter', description: 'Open highlighted session (empty prompt)', group: 'compose' },
  { id: 'compose.newline', keys: 'Shift+Enter', description: 'Insert a newline', group: 'compose' },
  { id: 'compose.jump', keys: 'Alt+1–9', description: 'Open session by number', group: 'compose' },
  { id: 'compose.deselect', keys: 'Esc', description: 'Clear the prompt and deselect', group: 'compose' },
  { id: 'compose.help', keys: '?', description: 'Open this help panel', group: 'compose' },
  // ── In-session — DISPLAY-ONLY ────────────────────────────────────────
  { id: 'insession.detach', keys: '← / Esc', description: 'Detach back to the session list', group: 'in-session' },
  { id: 'insession.forceDetach', keys: 'Ctrl+Z', description: 'Force-detach to the list', group: 'in-session' },
  { id: 'insession.permMode', keys: 'Shift+Tab', description: 'Cycle permission mode', group: 'in-session' },
]

export function matchListShortcut(e: KeyboardLike): ListRowAction | undefined {
  return SHORTCUTS.find((s) => s.match?.(e))?.action
}

export function shortcutsByGroup(group: ShortcutGroup): Shortcut[] {
  return SHORTCUTS.filter((s) => s.group === group)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/lib/shortcuts.test.ts`
Expected: PASS.

- [ ] **Step 5: Typecheck + commit**

Run: `npx tsc -b`
```bash
git add web/src/lib/shortcuts.ts web/src/lib/shortcuts.test.ts
git commit -m "feat(web): shortcuts registry for list-focus mode"
```

---

### Task 2: SessionCard — list-mode keydown + focus-self

**Files:**
- Modify: `web/src/components/SessionCard.tsx` (props ~20-34; `handleKeyDown` ~57-62; add focus effect; button ~97-108)
- Test: `web/src/components/__tests__/SessionCard.test.tsx` (create if absent)

**Interfaces:**
- Consumes: `matchListShortcut`, `ListRowAction` from `@/lib/shortcuts`.
- Produces: `SessionCard` gains props `listMode?: boolean`, `onNavigate?: (dir: 'up' | 'down') => void`, `onExit?: () => void`. Its keydown handler routes registry actions to existing/new callbacks. When `listMode && selected`, the row button focuses itself.

- [ ] **Step 1: Write the failing test**

Create/extend `web/src/components/__tests__/SessionCard.test.tsx`. Use a minimal `Session`/`Account` factory (copy field shapes from an existing test such as `KeyboardSessions.test.tsx`):

```ts
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const SESSION = { id: 's1', name: 'Alpha', accountName: 'acct', status: 'ready', updatedAt: null, subAgents: [], latestSuggestions: [] } as unknown as Session

function props(over = {}) {
  return {
    session: SESSION, account: undefined, selected: true, listMode: true,
    onOpen: vi.fn(), onRename: vi.fn(), onClose: vi.fn(), onDelete: vi.fn(),
    onQuickResume: vi.fn(), onNavigate: vi.fn(), onExit: vi.fn(), ...over,
  }
}

async function press(key: string) {
  const user = userEvent.setup()
  screen.getByRole('button', { name: /open session alpha/i }).focus()
  await user.keyboard(key)
}

describe('SessionCard — list-mode keys', () => {
  it('c continues (resume), r renames, a archives, d deletes', async () => {
    const onQuickResume = vi.fn(), onRename = vi.fn(), onClose = vi.fn(), onDelete = vi.fn()
    render(<SessionCard {...props({ onQuickResume, onRename, onClose, onDelete })} />)
    await press('c'); expect(onQuickResume).toHaveBeenCalledWith('s1', 'continue')
    await press('r'); expect(onRename).toHaveBeenCalledWith('s1')
    await press('a'); expect(onClose).toHaveBeenCalledWith('s1')
    await press('{Backspace}'); expect(onClose).toHaveBeenCalledTimes(2)
    await press('d'); expect(onDelete).toHaveBeenCalledWith('s1')
    await press('{Delete}'); expect(onDelete).toHaveBeenCalledTimes(2)
  })

  it('Enter opens, arrows navigate, Esc exits', async () => {
    const onOpen = vi.fn(), onNavigate = vi.fn(), onExit = vi.fn()
    render(<SessionCard {...props({ onOpen, onNavigate, onExit })} />)
    await press('{Enter}'); expect(onOpen).toHaveBeenCalledWith('s1')
    await press('{ArrowDown}'); expect(onNavigate).toHaveBeenCalledWith('down')
    await press('{ArrowUp}'); expect(onNavigate).toHaveBeenCalledWith('up')
    await press('{Escape}'); expect(onExit).toHaveBeenCalled()
  })

  it('focuses the row when listMode + selected', () => {
    render(<SessionCard {...props({ listMode: true, selected: true })} />)
    expect(screen.getByRole('button', { name: /open session alpha/i })).toHaveFocus()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/SessionCard.test.tsx`
Expected: FAIL — props don't exist; only Enter/Space handled.

- [ ] **Step 3: Implement**

In `SessionCard.tsx`:

Add to the import line for shortcuts:
```tsx
import { matchListShortcut } from '@/lib/shortcuts'
```

Extend `SessionCardProps` (after `onQuickResume`):
```tsx
  /** When true, the highlighted row takes real DOM focus (list-focus mode). */
  listMode?: boolean
  /** Move the highlight to the previous/next row (list mode). */
  onNavigate?: (dir: 'up' | 'down') => void
  /** Leave list mode and return focus to the prompt. */
  onExit?: () => void
```

Update the destructure:
```tsx
export function SessionCard({ session, account, selected = false, listMode = false, onOpen, onRename, onClose, onDelete, onQuickResume, onNavigate, onExit }: SessionCardProps) {
```

Add a ref for the main button (next to `articleRef`):
```tsx
  const buttonRef = useRef<HTMLButtonElement>(null)
```

Add a focus effect after the existing scroll-into-view effect:
```tsx
  // In list-focus mode, give the highlighted row REAL keyboard focus so single-key
  // shortcuts land here (and never type into the prompt).
  useEffect(() => {
    if (listMode && selected) buttonRef.current?.focus()
  }, [listMode, selected])
```

Replace `handleKeyDown` (lines ~57-62):
```tsx
  const resumeText = suggestions[0] ?? 'continue' // (already defined above as `resumeText`)

  function handleKeyDown(e: React.KeyboardEvent) {
    const action = matchListShortcut(e)
    if (!action) return
    e.preventDefault()
    switch (action.type) {
      case 'open': onOpen(session.id); break
      case 'continue': onQuickResume(session.id, resumeText); break
      case 'rename': onRename(session.id); break
      case 'archive': onClose(session.id); break
      case 'permanent-delete': onDelete(session.id); break
      case 'navigate': onNavigate?.(action.dir); break
      case 'exit': onExit?.(); break
    }
  }
```
> `resumeText` is already declared earlier in the component (line ~45) — do not redeclare it; the comment above just marks where it comes from. Use the existing one.

Attach the ref to the main button (line ~97, add `ref={buttonRef}`):
```tsx
      <button
        type="button"
        id={`row-${session.id}`}
        ref={buttonRef}
        tabIndex={selected ? 0 : -1}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/components/__tests__/SessionCard.test.tsx`
Expected: PASS.

- [ ] **Step 5: Typecheck + lint + commit**

Run: `npx tsc -b && npx eslint src/components/SessionCard.tsx`
Expected: SessionList call sites still typecheck (new props are optional). 
```bash
git add web/src/components/SessionCard.tsx web/src/components/__tests__/SessionCard.test.tsx
git commit -m "feat(web): SessionCard list-mode single-key handler + focus-self"
```

---

### Task 3: SessionList — thread list-mode props

**Files:**
- Modify: `web/src/components/SessionList.tsx` (props ~8-27; `renderCard` ~101-113)
- Test: covered via Task 5/6 integration; no separate unit test required.

**Interfaces:**
- Consumes: SessionCard's new props (Task 2).
- Produces: `SessionList` accepts `listMode?: boolean`, `onNavigate?: (dir: 'up' | 'down') => void`, `onExit?: () => void` and forwards them to every `SessionCard`.

- [ ] **Step 1: Add props to the interface**

In `SessionListProps` (after `onQuickResume`):
```tsx
  listMode?: boolean
  onNavigate?: (dir: 'up' | 'down') => void
  onExit?: () => void
```

- [ ] **Step 2: Destructure + forward**

Add `listMode, onNavigate, onExit` to the destructured params, and pass them in `renderCard`:
```tsx
  const renderCard = (session: Session) => (
    <SessionCard
      key={session.id}
      session={session}
      account={accountMap.get(session.accountName)}
      selected={session.id === selectedId}
      listMode={listMode}
      onOpen={onOpen}
      onRename={onRename}
      onClose={onClose}
      onDelete={onDelete}
      onQuickResume={onQuickResume}
      onNavigate={onNavigate}
      onExit={onExit}
    />
  )
```

- [ ] **Step 3: Typecheck + commit**

Run: `npx tsc -b`
```bash
git add web/src/components/SessionList.tsx
git commit -m "feat(web): SessionList forwards list-mode props to cards"
```

---

### Task 4: DispatchBar — Tab enters list mode; remove Ctrl+R/Ctrl+X + press-twice hint

**Files:**
- Modify: `web/src/components/DispatchBar.tsx` (props ~11-36; `handleKeyDown` ~99-172; hint JSX ~247-256)
- Test: `web/src/components/__tests__/DispatchBar.test.tsx`

**Interfaces:**
- Consumes: nothing new.
- Produces: `DispatchBar` props change — **remove** `onRowAction` and `confirmDeleteName`; **add** `onEnterList: () => void` and `onJump: (index: number) => void`. `Tab` on an empty prompt fires `onEnterList`; `Alt+1-9` fires `onJump`. `Ctrl+R`/`Ctrl+X` no longer handled.

- [ ] **Step 1: Update tests (failing)**

In `DispatchBar.test.tsx`:
- In `makeProps` (lines ~13-27): remove `onRowAction: vi.fn(),` and add `onEnterList: vi.fn(), onJump: vi.fn(),`.
- Replace the `Alt+2 jumps…` test body to use `onJump`:

```ts
  it('Alt+2 jumps to row index 1', async () => {
    const user = userEvent.setup()
    const onJump = vi.fn()
    render(<DispatchBar {...makeProps({ onJump })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}2{/Alt}')
    expect(onJump).toHaveBeenCalledWith(1)
  })
```

- Delete the four `Ctrl+R …` / `Ctrl+X …` tests (~184-222) and add:

```ts
  it('Tab on an empty prompt enters list mode', async () => {
    const user = userEvent.setup()
    const onEnterList = vi.fn()
    render(<DispatchBar {...makeProps({ onEnterList })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Tab}')
    expect(onEnterList).toHaveBeenCalledTimes(1)
  })

  it('Tab with a draft does NOT enter list mode (native focus move)', async () => {
    const user = userEvent.setup()
    const onEnterList = vi.fn()
    render(<DispatchBar {...makeProps({ onEnterList })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'draft')
    await user.keyboard('{Tab}')
    expect(onEnterList).not.toHaveBeenCalled()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/DispatchBar.test.tsx`
Expected: FAIL — `onEnterList`/`onJump` not wired; Tab not handled.

- [ ] **Step 3: Update props**

In `DispatchBar.tsx` props interface: **delete** the `onRowAction` prop (lines ~21-24) and the `confirmDeleteName` prop (~29-30). Add:
```tsx
  /** Tab on an empty prompt — drop into list-focus mode. */
  onEnterList: () => void
  /** Alt+1..9 — open session by index. */
  onJump: (index: number) => void
```
Update the destructure accordingly (remove `onRowAction`, `confirmDeleteName`; add `onEnterList`, `onJump`).

- [ ] **Step 4: Update `handleKeyDown`**

Replace the `Alt+Digit` block to call `onJump`:
```tsx
    if (e.altKey && /^[1-9]$/.test(e.key)) {
      e.preventDefault()
      onJump(Number(e.key) - 1)
      return
    }
```

**Delete** the `Ctrl+R` block (~157-163) and the `Ctrl+X` block (~165-171). After the `ArrowRight` block, add the Tab-to-enter branch:
```tsx
    // Tab on an empty prompt → enter list-focus mode. With a draft, Tab keeps its
    // native job (move focus to the Create button) so keyboard send still works.
    if (empty && e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault()
      onEnterList()
      return
    }
```

- [ ] **Step 5: Remove the press-twice hint**

Delete the `confirmDeleteName` hint JSX (lines ~247-256, the `{confirmDeleteName && (…)}` block).

- [ ] **Step 6: Run test to verify it fails (on call sites) / passes (unit)**

Run: `npx vitest run src/components/__tests__/DispatchBar.test.tsx`
Expected: PASS for DispatchBar's own tests. `npx tsc -b` will now error in Dashboard/DesktopShell (they still pass `onRowAction`/`confirmDeleteName`) — fixed in Tasks 5 & 6. Implement Tasks 4-6 together before the `tsc -b` gate, or commit with the note below.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/DispatchBar.tsx web/src/components/__tests__/DispatchBar.test.tsx
git commit -m "feat(web): DispatchBar Tab enters list mode; drop Ctrl+R/X + press-twice hint"
```
> The `onRowAction`/`confirmDeleteName` removal breaks the two shells until Tasks 5-6 land. If each commit must typecheck, implement Tasks 4-6 as one commit.

---

### Task 5: Dashboard — list-mode state + wiring; remove press-twice guard

**Files:**
- Modify: `web/src/components/Dashboard.tsx` (`SessionList` ~1810-1826; `DispatchBar` ~1830-1848; `handleRowAction` ~1485-1530; `pendingDelete` state ~1341-1349, `clearPendingDelete` ~1532-1538; `handleNavigate` ~1465-1477; `autoFocusKey` ~1846)
- Test: `web/src/components/__tests__/KeyboardSessions.test.tsx`

**Interfaces:**
- Consumes: `DispatchBar` (`onEnterList`, `onJump`), `SessionList` (`listMode`, `onNavigate`, `onExit`) from Tasks 3-4.
- Produces: Dashboard owns `listMode` state; `Tab` enters it; list-mode actions route to existing handlers.

- [ ] **Step 1: Update integration tests (failing)**

In `KeyboardSessions.test.tsx`:
- Rewrite the rename test to go through list mode:
```ts
  it('Tab then r opens the rename dialog for the highlighted session', async () => {
    const user = userEvent.setup()
    renderDashboard()
    await user.keyboard('{ArrowDown}') // highlight first row (compose mode)
    await user.keyboard('{Tab}')       // enter list mode (row gets focus)
    await user.keyboard('r')
    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
  })
```
- Replace the three `Ctrl+X` press-twice tests (~228-266) with a single-press archive test:
```ts
  it('Tab then a archives the highlighted session (single press)', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useCloseSession).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useCloseSession>)
    const user = userEvent.setup()
    renderDashboard()
    await user.keyboard('{ArrowDown}{Tab}a')
    expect(mutate).toHaveBeenCalledWith('s1') // first session id from the test fixture
  })
```
- Add continue + delete:
```ts
  it('Tab then c sends "continue"', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useSendPrompt).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useSendPrompt>)
    const user = userEvent.setup()
    renderDashboard()
    await user.keyboard('{ArrowDown}{Tab}c')
    expect(mutate).toHaveBeenCalledWith(expect.objectContaining({ text: 'continue' }))
  })

  it('Tab then d opens the permanent-delete dialog', async () => {
    const user = userEvent.setup()
    renderDashboard()
    await user.keyboard('{ArrowDown}{Tab}d')
    expect(screen.getByRole('dialog', { name: /delete/i })).toBeInTheDocument()
  })
```
> Match `renderDashboard`/`NOOP_MUTATION`/the mocked hook names to this file's existing harness; add `useSendPrompt` to the `@/api/hooks` mock object if it isn't already there. Adjust the expected session id to the fixture's first session.

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/KeyboardSessions.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Remove press-twice machinery**

In `Dashboard.tsx`: delete the `pendingDelete` state + `pendingDeleteTimeoutRef` + its unmount cleanup effect (~1341-1349), the `clearPendingDelete` function (~1532-1538), and the entire `handleRowAction` function (~1485-1530). In `handleNavigate` (~1465-1477) remove the `setPendingDelete(null)` / timeout-clear lines, leaving just the `highlightedIndex` update and the empty-list guard.

- [ ] **Step 4: Add list-mode state + handlers**

Add near the other `useState` calls (e.g. by `highlightedIndex`):
```tsx
  const [listMode, setListMode] = useState(false)
  const [focusNonce, setFocusNonce] = useState(0)

  function handleEnterList() {
    setListMode(true)
    if (highlightedIndex < 0) setHighlightedIndex(0)
  }
  function handleExitList() {
    setListMode(false)
    setFocusNonce((n) => n + 1) // pull focus back to the prompt
  }
  function handleJump(index: number) {
    const s = orderedSessions[index]
    if (s) { setHighlightedIndex(index); handleOpen(s.id) }
  }
```

- [ ] **Step 5: Rewire `SessionList` + `DispatchBar`**

In the `<SessionList>` (after `onQuickResume={handleQuickResume}`) add:
```tsx
            listMode={listMode}
            onNavigate={handleNavigate}
            onExit={handleExitList}
```

In the `<DispatchBar>`: remove `onRowAction={handleRowAction}` and the `confirmDeleteName={…}` prop; change `onEscape` to drop `clearPendingDelete`; add the new props; change `autoFocusKey`:
```tsx
            onNavigate={handleNavigate}
            onOpenHighlighted={handleOpenHighlighted}
            onEnterList={handleEnterList}
            onJump={handleJump}
            onShowHelp={() => setShowHelp(true)}
            onEscape={() => setHighlightedIndex(-1)}
            activeDescendantId={selectedId ? `row-${selectedId}` : undefined}
            autoFocusKey={`${String(focusDispatchSignal)}:${focusNonce}`}
            isPending={createSession.isPending}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `npx vitest run src/components/__tests__/KeyboardSessions.test.tsx`
Expected: PASS.

- [ ] **Step 7: Typecheck + commit**

Run: `npx tsc -b` (DesktopShell may still error until Task 6).
```bash
git add web/src/components/Dashboard.tsx web/src/components/__tests__/KeyboardSessions.test.tsx
git commit -m "feat(web): Dashboard list-focus mode wiring; remove press-twice guard"
```

---

### Task 6: DesktopShell — mirror Dashboard wiring

**Files:**
- Modify: `web/src/components/DesktopShell.tsx` (`SessionList` ~401-420; `DispatchBar` ~441-459; `handleRowAction` ~269-298; `pendingDelete` state ~123 area; `handleNavigate`; `dispatchFocusNonce` ~131)
- Test: `web/src/components/__tests__/DesktopShell.test.tsx`

**Interfaces:**
- Consumes: same as Task 5.
- Produces: DesktopShell owns `listMode`; mirrors Dashboard. Note DesktopShell uses `focusSession` for open, `handleDelete` for delete, and already has `dispatchFocusNonce`/`setDispatchFocusNonce`.

- [ ] **Step 1: Write/adjust a failing test**

In `DesktopShell.test.tsx`, add (using the file's render helper + hook mocks; mirror `KeyboardSessions.test.tsx` if no harness exists):
```ts
  it('Tab then a archives the highlighted session', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useCloseSession).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useCloseSession>)
    const user = userEvent.setup()
    renderDesktopShell()
    await user.keyboard('{ArrowDown}{Tab}a')
    expect(mutate).toHaveBeenCalled()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/DesktopShell.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Remove press-twice machinery**

Delete the `pendingDelete` state/ref, `clearPendingDelete`, and the `handleRowAction` function (~269-298). Strip pending-delete lines from `handleNavigate`.

- [ ] **Step 4: Add list-mode state + handlers**

```tsx
  const [listMode, setListMode] = useState(false)

  function handleEnterList() {
    setListMode(true)
    if (highlightedIndex < 0) setHighlightedIndex(0)
  }
  function handleExitList() {
    setListMode(false)
    setDispatchFocusNonce((n) => n + 1)
  }
  function handleJump(index: number) {
    const s = orderedSessions[index]
    if (s) { setHighlightedIndex(index); focusSession(s.id) }
  }
```
> `highlightedIndex`/`setHighlightedIndex` and `orderedSessions` already exist in DesktopShell; reuse them. `setDispatchFocusNonce` already exists (line 131).

- [ ] **Step 5: Rewire `SessionList` + `DispatchBar`**

In `<SessionList>` add `listMode={listMode}`, `onNavigate={handleNavigate}`, `onExit={handleExitList}`.

In `<DispatchBar>`: remove `onRowAction={handleRowAction}` and `confirmDeleteName={…}`; set `onEscape={() => setHighlightedIndex(-1)}`; add `onEnterList={handleEnterList}` and `onJump={handleJump}`. (`autoFocusKey={dispatchFocusNonce}` already bumps on exit since `handleExitList` increments it.)

- [ ] **Step 6: Run test + full typecheck + commit**

Run: `npx vitest run src/components/__tests__/DesktopShell.test.tsx && npx tsc -b && npx eslint src/components/Dashboard.tsx src/components/DesktopShell.tsx`
Expected: PASS; typecheck/eslint clean.
```bash
git add web/src/components/DesktopShell.tsx web/src/components/__tests__/DesktopShell.test.tsx
git commit -m "feat(web): DesktopShell list-focus mode wiring"
```

---

### Task 7: Help slide-out (replace modal) + visible button + global '?'

**Files:**
- Create: `web/src/components/HelpDrawer.tsx`, `web/src/components/useGlobalHelpKey.ts`
- Modify: `web/src/components/Dashboard.tsx` (import/render ~1984; help button + `useGlobalHelpKey`), `web/src/components/DesktopShell.tsx` (import ~27, render ~513; help button + hook)
- Delete: `web/src/components/ShortcutsHelp.tsx`, `web/src/components/__tests__/ShortcutsHelp.test.tsx`
- Test: `web/src/components/__tests__/HelpDrawer.test.tsx`, `web/src/components/__tests__/useGlobalHelpKey.test.tsx`

**Interfaces:**
- Consumes: `SHORTCUTS`, `shortcutsByGroup` (Task 1).
- Produces: `function HelpDrawer({ onClose }: { onClose: () => void })`; `function useGlobalHelpKey({ enabled, onShowHelp }: { enabled: boolean; onShowHelp: () => void }): void`.

- [ ] **Step 1: Write the HelpDrawer test (failing)**

Create `web/src/components/__tests__/HelpDrawer.test.tsx`:
```ts
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HelpDrawer } from '../HelpDrawer'
import { SHORTCUTS } from '@/lib/shortcuts'

describe('HelpDrawer', () => {
  it('is a labelled dialog with a row per registry shortcut (drift guard)', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    expect(screen.getByRole('dialog', { name: /help/i })).toBeInTheDocument()
    for (const s of SHORTCUTS) expect(screen.getByText(s.description)).toBeInTheDocument()
  })
  it('shows list-mode keys and not the old Ctrl bindings', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    expect(screen.getByText('c')).toBeInTheDocument()
    expect(screen.getByText('a / Backspace')).toBeInTheDocument()
    expect(screen.queryByText('Ctrl+X')).not.toBeInTheDocument()
  })
  it('has Navigation and Tips sections', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    expect(screen.getByRole('heading', { name: /navigation/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /tips/i })).toBeInTheDocument()
  })
  it('Escape and backdrop click close', async () => {
    const onClose = vi.fn(); const user = userEvent.setup()
    render(<HelpDrawer onClose={onClose} />)
    await user.keyboard('{Escape}')
    await user.click(screen.getByTestId('help-drawer-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/HelpDrawer.test.tsx`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement HelpDrawer**

Create `web/src/components/HelpDrawer.tsx` (reuse the Esc + Tab-trap pattern from `ShortcutsHelp.tsx` lines 36-73; render a right slide-out, source rows from the registry):

```tsx
import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { shortcutsByGroup, type Shortcut } from '@/lib/shortcuts'

const NAV_MAP = [
  { name: 'Sessions', detail: 'Live sessions list — the default view' },
  { name: 'Accounts', detail: 'Manage Claude accounts and rotation' },
  { name: 'Archived', detail: 'Review, unarchive, or permanently delete past sessions' },
  { name: 'Session view', detail: 'Open a session to read its transcript and send prompts' },
  { name: 'Review', detail: 'Read-only transcript of an archived session' },
]
const TIPS = [
  { title: 'List-focus mode', detail: 'Press Tab on an empty prompt to focus the list; then c/r/a/d act on the row. Esc returns to the prompt.' },
  { title: 'Accounts = config dirs', detail: 'Each account is an isolated CLAUDE_CONFIG_DIR; ccswitch rotates them as usage allows.' },
  { title: 'Archive vs delete', detail: 'Archive (a) hides a session but keeps it; delete (d) removes it permanently.' },
  { title: 'Permission modes', detail: 'Inside a session, Shift+Tab cycles the permission mode.' },
]

export function HelpDrawer({ onClose }: { onClose: () => void }) {
  const panelRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { closeButtonRef.current?.focus() }, [])
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key === 'Tab') {
        const f = panelRef.current?.querySelectorAll<HTMLElement>('button, [tabindex]:not([tabindex="-1"])')
        if (!f || f.length === 0) return
        const first = f[0], last = f[f.length - 1]
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/50"
      data-testid="help-drawer-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="help-drawer-title"
        className={cn('h-full w-full max-w-md overflow-y-auto p-6 space-y-6',
          'bg-white dark:bg-gray-800 border-l border-gray-200 dark:border-gray-600 shadow-xl')}
      >
        <div className="flex items-center justify-between">
          <h2 id="help-drawer-title" className="text-lg font-semibold text-gray-900 dark:text-gray-100">Help &amp; Shortcuts</h2>
          <button ref={closeButtonRef} type="button" aria-label="Close help" onClick={onClose}
            className={cn('h-8 w-8 flex items-center justify-center rounded-md text-gray-500 dark:text-gray-400',
              'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500')}>
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <Section title="Compose mode" bindings={shortcutsByGroup('compose')} />
        <Section title="List-focus mode" bindings={shortcutsByGroup('list-mode')} />
        <Section title="In-session" bindings={shortcutsByGroup('in-session')} />
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">Navigation</h3>
          <dl className="space-y-1">
            {NAV_MAP.map((n) => (
              <div key={n.name} className="flex items-baseline gap-3">
                <dt className="shrink-0 min-w-[110px] text-sm font-medium text-gray-700 dark:text-gray-300">{n.name}</dt>
                <dd className="text-sm text-gray-600 dark:text-gray-400">{n.detail}</dd>
              </div>
            ))}
          </dl>
        </section>
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">Tips</h3>
          <dl className="space-y-2">
            {TIPS.map((t) => (
              <div key={t.title}>
                <dt className="text-sm font-medium text-gray-700 dark:text-gray-300">{t.title}</dt>
                <dd className="text-sm text-gray-600 dark:text-gray-400">{t.detail}</dd>
              </div>
            ))}
          </dl>
        </section>
      </div>
    </div>
  )
}

function Section({ title, bindings }: { title: string; bindings: Shortcut[] }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">{title}</h3>
      <dl className="space-y-1">
        {bindings.map((b) => (
          <div key={b.id} className="flex items-baseline gap-3">
            <dt className="shrink-0 min-w-[120px]">
              <kbd className={cn('inline-block rounded px-1.5 py-0.5 text-xs font-mono',
                'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 border border-gray-300 dark:border-gray-600')}>{b.keys}</kbd>
            </dt>
            <dd className="text-sm text-gray-700 dark:text-gray-300">{b.description}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
```

- [ ] **Step 4: Run HelpDrawer test**

Run: `npx vitest run src/components/__tests__/HelpDrawer.test.tsx`
Expected: PASS.

- [ ] **Step 5: Global help-key hook (test + impl)**

Create `web/src/components/__tests__/useGlobalHelpKey.test.tsx`:
```tsx
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useGlobalHelpKey } from '../useGlobalHelpKey'

function Harness({ onShowHelp }: { onShowHelp: () => void }) {
  useGlobalHelpKey({ enabled: true, onShowHelp })
  return <input aria-label="field" />
}

describe('useGlobalHelpKey', () => {
  it('opens help on ? when not in a field', async () => {
    const onShowHelp = vi.fn(); const user = userEvent.setup()
    render(<Harness onShowHelp={onShowHelp} />)
    await user.keyboard('{Shift>}/{/Shift}')
    expect(onShowHelp).toHaveBeenCalledTimes(1)
  })
  it('does NOT open help when typing ? into an input', async () => {
    const onShowHelp = vi.fn(); const user = userEvent.setup()
    const { getByLabelText } = render(<Harness onShowHelp={onShowHelp} />)
    await user.click(getByLabelText('field'))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(onShowHelp).not.toHaveBeenCalled()
  })
})
```

Run: `npx vitest run src/components/__tests__/useGlobalHelpKey.test.tsx` → FAIL (missing module). Then create `web/src/components/useGlobalHelpKey.ts`:
```ts
import { useEffect } from 'react'

interface Options { enabled: boolean; onShowHelp: () => void }

/** `?` opens help from anywhere — except while typing in a field or with a dialog open. */
export function useGlobalHelpKey({ enabled, onShowHelp }: Options): void {
  useEffect(() => {
    if (!enabled) return
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== '?' && !(e.key === '/' && e.shiftKey)) return
      if (e.defaultPrevented) return
      const t = e.target as HTMLElement | null
      const tag = t?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable) return
      if (document.querySelector('[role="dialog"]')) return
      e.preventDefault()
      onShowHelp()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [enabled, onShowHelp])
}
```
Run the hook test again → PASS.

- [ ] **Step 6: Swap modal → drawer + button + global '?' in both shells**

In **Dashboard.tsx**: replace `import { ShortcutsHelp }` with `import { HelpDrawer } from './HelpDrawer'`; add `import { useGlobalHelpKey } from './useGlobalHelpKey'`; add `HelpCircle` to the `lucide-react` import. Replace the render: `{showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}`. Near `useGlobalListNav(...)` add `useGlobalHelpKey({ enabled: activeTab === 'sessions', onShowHelp: () => setShowHelp(true) })`. Add a visible help button in the header chrome:
```tsx
        <button type="button" aria-label="Open help" onClick={() => setShowHelp(true)}
          className={cn('h-9 w-9 flex items-center justify-center rounded-md text-gray-500 dark:text-gray-400',
            'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500')}>
          <HelpCircle size={18} aria-hidden="true" />
        </button>
```

In **DesktopShell.tsx**: same swaps (import `HelpDrawer` + `useGlobalHelpKey` + `HelpCircle`); replace render at ~513 with `{showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}`; add `useGlobalHelpKey({ enabled: tab === 'sessions', onShowHelp: () => setShowHelp(true) })`; add the same help button to the sidebar/header chrome.

- [ ] **Step 7: Delete the old modal**

```bash
git rm web/src/components/ShortcutsHelp.tsx web/src/components/__tests__/ShortcutsHelp.test.tsx
```
Run: `grep -rn "ShortcutsHelp" web/src` → expect no output.

- [ ] **Step 8: Full verification**

Run from `web/`:
```bash
npx tsc -b
npx vitest run
npx eslint src/components/Dashboard.tsx src/components/DesktopShell.tsx src/components/HelpDrawer.tsx src/components/useGlobalHelpKey.ts src/components/SessionCard.tsx src/components/SessionList.tsx src/components/DispatchBar.tsx
```
Expected: typecheck clean; full suite green; eslint clean.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(web): global help slide-out replaces shortcuts modal; visible button + global ?"
```

---

## Self-Review

**Spec coverage:**
- Two modes; Tab-in / Esc-out → Tasks 4 (DispatchBar Tab, exit keys via registry), 5/6 (state). ✓
- List-mode keys c/r/a(Backspace)/d(Delete)/Enter-→-Space/arrows/Esc-←-Tab → Task 1 (registry) + Task 2 (SessionCard). ✓
- Real focus on highlighted row in list mode → Task 2 focus effect + `buttonRef`. ✓
- `Ctrl+R`/`Ctrl+X` removed → Task 4. ✓
- Press-twice guard removed; archive single-press → Tasks 4 (hint), 5/6 (state/handler removal). ✓
- Delete routes through `PermanentDeleteDialog` → Task 2 (`onDelete` → existing `handleSessionDelete`/`handleDelete`). ✓
- Continue sends `continue` via `useSendPrompt` → Task 2 (`onQuickResume(id, resumeText)`). ✓
- Single registry feeds handler + help (no drift) → Task 1 + Task 2 (matchListShortcut) + Task 7 (drift-guard test). ✓
- Help slide-out replaces modal, 3 sections, global `?` + button → Task 7; old modal deleted. ✓
- Both shells updated → Tasks 5, 6, 7. ✓

**Placeholder scan:** No TBD/TODO; full code in every code step. Test-harness names (`renderDashboard`, `NOOP_MUTATION`, `renderDesktopShell`) are flagged to match each file's existing harness rather than invented.

**Type consistency:** `ListRowAction`/`KeyboardLike` defined in Task 1, consumed in Tasks 2 & 7; SessionCard's new optional props (Task 2) are forwarded by SessionList (Task 3) and supplied by both shells (Tasks 5/6); DispatchBar's `onRowAction`→(`onEnterList`+`onJump`) and `confirmDeleteName` removal are applied at both call sites (Tasks 5/6).

**Commit-green note:** The DispatchBar prop changes (Task 4) break the shells until Tasks 5-6 land. If each commit must typecheck, implement Tasks 4-6 as one commit.
