# Keyboard-First Sessions ("Agent View" for the ccswitch web dashboard)

## Goal

Replicate Claude Code's keyboard-first "Agent View" in the ccswitch web dashboard:

- **Part A — Bottom dispatch slot:** a persistent prompt input pinned at the bottom of the
  Sessions tab, with an inline repo selector that defaults to the last-chosen repo
  (persisted in `localStorage`). Typing + **Enter** creates a new session
  (`{ initialPrompt, repo, branch }`) and opens it. `Shift+Enter` = newline. The slot
  auto-focuses whenever the dashboard is shown and re-focuses after returning from a session.
- **Part B — Keyboard list navigation (prompt stays focused):** `↑/↓` move a highlighted
  session in the list above the prompt (boundary-aware — only navigate when the caret is at the
  first/last line). `Enter` on an **empty** prompt (or `→`) opens the highlighted session.
  `Esc` clears the prompt and de-highlights. Row actions on the highlighted session:
  `r`/`F2` rename, `Delete` delete (reuses the existing confirm dialog), `Alt+1`–`Alt+9` jump.
  `?` opens a shortcuts cheat-sheet modal. The highlighted row gets a visible selection style,
  `aria-selected`, roving focus, and stays scrolled into view.
- **Part C — In-session shortcuts:** `Esc` → back to the session list; `Shift+Tab` → cycle the
  permission mode. (Enter submit / Shift+Enter newline already exist — left untouched.)

Arrows-only — **no `j`/`k`**. Out of scope (terminal-only): vim mode, peek panel, voice,
`Ctrl+R` history search, Alt-meta word motions.

## Architecture

- **No router, no store.** View switching is React local state in `web/src/App.tsx`
  (`type View = { type: 'dashboard' } | { type: 'session'; sessionId; originElementId? }`,
  `App.tsx:13-15`; `handleOpenSession` `App.tsx:26-29`; `handleBack` `App.tsx:31-42`). The only
  global provider is `QueryClientProvider` (`web/src/main.tsx`). Data is TanStack Query only.
- **Dashboard** (`web/src/components/Dashboard.tsx`) owns the two-tab UI
  (`TabId = 'sessions' | 'accounts'`, `Dashboard.tsx:802`; `activeTab` state `:821`; tablist
  arrow-nav `handleTabKeyDown` `:841-850`; Sessions tabpanel `:1004-1050`). It already holds
  `useSessions()` `:811`, `useCreateSession()` `:814`, `useRenameSession` `:815`,
  `useCloseSession` `:816`, the New Session dialog (`NewSessionDialog` `:43-313`), the Rename
  dialog (`RenameDialog` `:325`), and `handleCreateSubmit` `:867-886`.
- **Selection state lives in `Dashboard`** (the owner of `sessions` + the dispatch slot + the
  list) and is threaded down to `SessionList` → `SessionCard`. This keeps a single source of
  truth for "which row is highlighted" and lets the dispatch slot (which keeps DOM focus) drive
  list navigation without moving focus — exactly the Claude Code trick.
- **DispatchBar** is a new presentational component rendered inside the Sessions tabpanel,
  *below* `SessionList`. It owns the prompt text, the inline repo `<select>`, and emits
  `onDispatch`, `onNavigate('up'|'down')`, `onOpenHighlighted`, `onActivateRowAction(...)`, and
  `onShowHelp` callbacks up to `Dashboard`. Boundary detection (caret on first/last line) lives
  in DispatchBar because only it can read the textarea selection.
- **In-session shortcuts** are added to `SessionView` (`web/src/components/session/SessionView.tsx`):
  `handleBack` `:140-143`, `handleSetMode` `:132-138`, `mode` state `:51` (defaults to
  `'interactive'`). `Shift+Tab` cycles through `PERMISSION_MODES` (`web/src/session/permissionModes.ts`)
  and calls `handleSetMode`.

## Tech Stack

- React 18 + TypeScript, Vite. `@tanstack/react-query` v5.
- Vitest (jsdom, globals, setup `web/src/test/setup.ts`) + `@testing-library/react` +
  `@testing-library/user-event`. Run: `cd web && pnpm test` (`vitest run`) or
  `pnpm vitest run src/components/__tests__/<file>.test.tsx`. Typecheck: `pnpm typecheck`.
  Lint: `pnpm lint` (`--max-warnings 0`).
- `cn()` from `@/lib/utils` (clsx + tailwind-merge) for class composition.
- Icons: `lucide-react`.
- Tests mock the API layer with `vi.mock('@/api/hooks', () => ({ ... }))`, providing every
  named export the component imports plus a `QK` object (see `DashboardTabs.test.tsx:9-33`).
  Mutations are stubbed as `{ mutate: vi.fn(), isPending: false, isSuccess: false }`.

## Global Constraints

- **Styling matches the existing codebase, NOT a semantic-token system.** `web/tailwind.config.js`
  has an **empty** `theme.extend` — there are no `bg-surface` / `text-content-*` tokens here.
  Every existing component uses raw paired utilities like
  `bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-200 dark:border-gray-700`
  and focus rings `focus-visible:ring-2 focus-visible:ring-blue-500`. **New components must mirror
  that exact pattern** (light class + `dark:` pair). Do not invent semantic tokens; do not
  hardcode hex. (This contradicts the KinDash-style "semantic token" instruction in the task —
  see "Contradictions" at the bottom of the originating brief; the repo's real convention wins.)
- **Accessibility:**
  - Highlighted row exposes `aria-selected` and uses roving focus semantics. The list `<ul>`
    becomes a single-select widget: give it `role="listbox"` + `aria-label`, each row's open
    button `role="option"` + `aria-selected`, and keep the active option scrolled into view via
    `scrollIntoView({ block: 'nearest' })`. Because DispatchBar keeps real DOM focus, set
    `aria-activedescendant` on the textarea to the highlighted row's id (`row-<id>`) so screen
    readers announce the moving selection without focus leaving the prompt.
  - The `?` help overlay is a `role="dialog"` `aria-modal="true"` with `aria-labelledby`, a focus
    trap, and `Esc`-to-close — **mirror the existing dialog pattern** in `Dashboard.tsx`
    (`NewSessionDialog` `:83-102`, `DeleteAccountDialog` `:415-434`: a `keydown` listener for
    `Escape` + `Tab` focus-cycling over `querySelectorAll('button, input, select, textarea, [tabindex]:not([tabindex="-1"])')`).
  - `min-h-[44px]` touch targets on any new interactive control, consistent with existing code.
  - Decorative icons get `aria-hidden="true"`.
- **Boundary-aware `↑/↓`:** in DispatchBar's textarea, `↑`/`↓` navigate the list **only** when
  the textarea is empty OR the caret is on the first line (for `↑`) / last line (for `↓`);
  otherwise let the browser move the cursor normally. Detect via `selectionStart`/`selectionEnd`
  vs. newline positions in `value` (helper `caretLineEdges(value, selStart) → { atFirstLine, atLastLine }`).
- **Arrows-only.** Never bind `j`/`k`.
- **Reuse the existing delete confirm path.** The dashboard already has `useCloseSession`
  (`hooks.ts:125`) wired as `handleClose` `:903-905` and surfaced via the SessionCard overflow
  "Close" item. `Delete` on the highlighted row must call the **same** `onClose(id)` path. (Note:
  there is no separate "delete session confirm dialog" today — closing a session is immediate via
  the overflow menu. The plan keeps that behavior: `Delete` = the existing `onClose`. The
  `DeleteAccountDialog` is for *accounts*, not sessions — do not repurpose it.)

## Edge Cases (must be handled + tested)

1. **Empty session list:** DispatchBar still works (can create a session). `↑/↓/→/Enter-empty`
   and row actions are no-ops (no highlighted row). `highlightedIndex` stays `-1`.
2. **Repo list still loading / errored:** the repo `<select>` shows "Loading…" / "— could not
   load repos —" (mirroring `NewSessionDialog` `:194-199`) and is `disabled` while loading, BUT
   if a `lastRepo` string is persisted, pre-seed the selected value optimistically so Enter can
   still dispatch with that repo string (the server accepts a `repo` string + clones on start).
3. **Highlighted row then deleted:** after `sessions` shrinks, clamp `highlightedIndex` to
   `Math.min(prev, sessions.length - 1)` (and `-1` when empty) in an effect keyed on
   `sessions.length`.
4. **Two tabs:** keyboard nav + dispatch slot apply to the **Sessions** tab only. The Accounts
   tab keeps its existing `handleTabKeyDown` arrow behavior (`Dashboard.tsx:841-850`). DispatchBar
   and selection are rendered only inside the `activeTab === 'sessions'` panel.

---

## Task 1 — `lastRepo` localStorage helper

**Files**
- `web/src/lib/lastRepo.ts` (new)
- `web/src/lib/__tests__/lastRepo.test.ts` (new)

**Interfaces**
- Produces:
  - `getLastRepo(): string | null` — returns the persisted `nameWithOwner`, or `null` if unset
    or if `localStorage` is unavailable / throws.
  - `setLastRepo(nameWithOwner: string): void` — persists; swallows quota/availability errors.
- Consumes: `window.localStorage`.

**TDD steps**

1. Write `web/src/lib/__tests__/lastRepo.test.ts`:

```ts
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { getLastRepo, setLastRepo } from '../lastRepo'

describe('lastRepo helper', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it('returns null when nothing is stored', () => {
    expect(getLastRepo()).toBeNull()
  })

  it('round-trips a repo nameWithOwner', () => {
    setLastRepo('cairnea/KinDash')
    expect(getLastRepo()).toBe('cairnea/KinDash')
  })

  it('overwrites the previous value', () => {
    setLastRepo('a/one')
    setLastRepo('b/two')
    expect(getLastRepo()).toBe('b/two')
  })

  it('returns null and does not throw when getItem throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(() => getLastRepo()).not.toThrow()
    expect(getLastRepo()).toBeNull()
  })

  it('does not throw when setItem throws (quota / private mode)', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceeded')
    })
    expect(() => setLastRepo('x/y')).not.toThrow()
  })
})
```

2. Run — fails (module missing).

3. Implement `web/src/lib/lastRepo.ts`:

```ts
/**
 * Tiny persistence helper for the dispatch bar's last-chosen repo.
 * Stores a GitHub `nameWithOwner` string. Net-new — nothing persisted this
 * value before. All access is wrapped so a blocked / unavailable localStorage
 * (private mode, quota, SSR) degrades to "no remembered repo" instead of crashing.
 */
const KEY = 'ccswitch.lastRepo'

export function getLastRepo(): string | null {
  try {
    return window.localStorage.getItem(KEY)
  } catch {
    return null
  }
}

export function setLastRepo(nameWithOwner: string): void {
  try {
    window.localStorage.setItem(KEY, nameWithOwner)
  } catch {
    /* ignore — persistence is best-effort */
  }
}
```

4. Run — passes.

---

## Task 2 — `DispatchBar` component (prompt + repo select, defaults to last repo, create-on-Enter, auto-focus)

**Files**
- `web/src/components/DispatchBar.tsx` (new)
- `web/src/components/__tests__/DispatchBar.test.tsx` (new)

**Interfaces**
- Consumes (props):

```ts
import type { GithubRepo } from '@/api/hooks'

export interface DispatchBarProps {
  repos: GithubRepo[]
  reposLoading: boolean
  reposError: boolean
  /** Fired on Enter when the prompt has text. */
  onDispatch: (input: { initialPrompt: string; repo: string | null }) => void
  /** ↑/↓ at a line boundary (or empty prompt). */
  onNavigate: (dir: 'up' | 'down') => void
  /** Enter on an EMPTY prompt, or →. */
  onOpenHighlighted: () => void
  /** r / F2 / Delete / Alt+1..9 on the highlighted row. */
  onRowAction: (action: { type: 'rename' } | { type: 'delete' } | { type: 'jump'; index: number }) => void
  /** ? — open the shortcuts overlay. */
  onShowHelp: () => void
  /** Esc — also clears highlight in the parent. */
  onEscape: () => void
  /** id of the highlighted row's button, e.g. "row-<id>", for aria-activedescendant. */
  activeDescendantId?: string
  /** Auto-focus the textarea when this changes / on mount. */
  autoFocusKey?: unknown
  isPending: boolean
}
```

- Produces: a textarea + repo `<select>`. Internal `text` state; repo selection seeded from
  `getLastRepo()` (Task 1) on mount, falling back to `''`.

**Key behaviors**
- Repo `<select>` mirrors `NewSessionDialog` `:185-207` (loading/error option text; options keyed
  by `nameWithOwner`). Default value = `getLastRepo()` if it is `''`-or-a-known repo; if loading
  but a `lastRepo` exists, keep the string selected optimistically.
- `onChange` of the select calls `setLastRepo(value)` immediately (also persisted on successful
  create by the parent — see Task 6).
- `handleKeyDown` on the textarea:
  - `Enter` + no `Shift`: `preventDefault()`. If `text.trim()` is non-empty →
    `onDispatch({ initialPrompt: text.trim(), repo: selectedRepo || null })`, then clear text.
    If empty → `onOpenHighlighted()`.
  - `Shift+Enter`: do nothing (browser inserts newline).
  - `ArrowUp`: if `text === '' || caretLineEdges(text, selStart).atFirstLine` →
    `preventDefault()` + `onNavigate('up')`; else default.
  - `ArrowDown`: symmetric with `atLastLine` → `onNavigate('down')`.
  - `ArrowRight`: if caret is at end of text → `preventDefault()` + `onOpenHighlighted()`; else default.
  - `Escape`: `preventDefault()`, clear text, `onEscape()`.
  - `r` or `F2` **only when `text === ''`**: `onRowAction({ type: 'rename' })`.
  - `Delete` **only when `text === ''`**: `onRowAction({ type: 'delete' })`.
  - `Alt+Digit1..9`: `preventDefault()` + `onRowAction({ type: 'jump', index: digit - 1 })`.
  - `?` (Shift+/): `preventDefault()` + `onShowHelp()`.
- Auto-focus: `useEffect` on `[autoFocusKey]` → `textareaRef.current?.focus()`.
- Set `aria-activedescendant={activeDescendantId}` on the textarea and `role="combobox"`-free —
  keep it a plain labelled textarea with `aria-controls` pointing at the listbox id
  (`"session-listbox"`).

Add the boundary helper at the top of the file (exported for unit testing):

```ts
export function caretLineEdges(value: string, caret: number): { atFirstLine: boolean; atLastLine: boolean } {
  const firstNewline = value.indexOf('\n')
  const atFirstLine = firstNewline === -1 || caret <= firstNewline
  const lastNewline = value.lastIndexOf('\n')
  const atLastLine = lastNewline === -1 || caret > lastNewline
  return { atFirstLine, atLastLine }
}
```

**TDD steps**

1. Write `web/src/components/__tests__/DispatchBar.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DispatchBar, caretLineEdges } from '../DispatchBar'
import type { GithubRepo } from '@/api/hooks'

const REPOS: GithubRepo[] = [
  { nameWithOwner: 'cairnea/KinDash', name: 'KinDash', owner: 'cairnea', isPrivate: true, url: '', defaultBranch: 'main', pushedAt: null, cloned: true },
  { nameWithOwner: 'acme/web', name: 'web', owner: 'acme', isPrivate: false, url: '', defaultBranch: 'main', pushedAt: null, cloned: false },
]

function makeProps(overrides: Partial<React.ComponentProps<typeof DispatchBar>> = {}) {
  return {
    repos: REPOS,
    reposLoading: false,
    reposError: false,
    onDispatch: vi.fn(),
    onNavigate: vi.fn(),
    onOpenHighlighted: vi.fn(),
    onRowAction: vi.fn(),
    onShowHelp: vi.fn(),
    onEscape: vi.fn(),
    isPending: false,
    ...overrides,
  }
}

describe('caretLineEdges', () => {
  it('single-line text is at both edges', () => {
    expect(caretLineEdges('hello', 2)).toEqual({ atFirstLine: true, atLastLine: true })
  })
  it('caret on first line of multi-line', () => {
    const v = 'a\nb\nc'
    expect(caretLineEdges(v, 0)).toEqual({ atFirstLine: true, atLastLine: false })
  })
  it('caret on last line of multi-line', () => {
    const v = 'a\nb\nc'
    expect(caretLineEdges(v, 5)).toEqual({ atFirstLine: false, atLastLine: true })
  })
  it('caret on a middle line is at neither edge', () => {
    const v = 'a\nb\nc'
    expect(caretLineEdges(v, 2)).toEqual({ atFirstLine: false, atLastLine: false })
  })
})

describe('DispatchBar', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.clearAllMocks()
  })

  it('renders a labelled prompt and a repo select', () => {
    render(<DispatchBar {...makeProps()} />)
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /repository/i })).toBeInTheDocument()
  })

  it('Enter with text dispatches and clears', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn()
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'fix the bug')
    await user.keyboard('{Enter}')
    expect(onDispatch).toHaveBeenCalledWith({ initialPrompt: 'fix the bug', repo: null })
    expect(box).toHaveValue('')
  })

  it('Shift+Enter inserts a newline (does not dispatch)', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn()
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'line1')
    await user.keyboard('{Shift>}{Enter}{/Shift}')
    await user.type(box, 'line2')
    expect(onDispatch).not.toHaveBeenCalled()
    expect(box).toHaveValue('line1\nline2')
  })

  it('Enter on empty prompt opens the highlighted session', async () => {
    const user = userEvent.setup()
    const onOpenHighlighted = vi.fn()
    render(<DispatchBar {...makeProps({ onOpenHighlighted })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Enter}')
    expect(onOpenHighlighted).toHaveBeenCalledTimes(1)
  })

  it('ArrowDown on empty prompt navigates down', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<DispatchBar {...makeProps({ onNavigate })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).toHaveBeenCalledWith('down')
  })

  it('ArrowUp navigates only at the first line, not from a lower line', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<DispatchBar {...makeProps({ onNavigate })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'a{Shift>}{Enter}{/Shift}b') // caret now on line 2
    await user.keyboard('{ArrowUp}')
    expect(onNavigate).not.toHaveBeenCalled() // moving cursor up within the textarea
  })

  it('Escape clears text and calls onEscape', async () => {
    const user = userEvent.setup()
    const onEscape = vi.fn()
    render(<DispatchBar {...makeProps({ onEscape })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'draft')
    await user.keyboard('{Escape}')
    expect(box).toHaveValue('')
    expect(onEscape).toHaveBeenCalled()
  })

  it('? opens the help overlay', async () => {
    const user = userEvent.setup()
    const onShowHelp = vi.fn()
    render(<DispatchBar {...makeProps({ onShowHelp })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(onShowHelp).toHaveBeenCalled()
  })

  it('Alt+2 jumps to row index 1', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}2{/Alt}')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'jump', index: 1 })
  })

  it('r on an empty prompt triggers rename action', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('r')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'rename' })
  })

  it('typing r as part of a prompt does NOT trigger rename', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'refactor')
    expect(onRowAction).not.toHaveBeenCalled()
    expect(box).toHaveValue('refactor')
  })

  it('seeds the repo select from getLastRepo()', () => {
    window.localStorage.setItem('ccswitch.lastRepo', 'acme/web')
    render(<DispatchBar {...makeProps()} />)
    expect(screen.getByRole('combobox', { name: /repository/i })).toHaveValue('acme/web')
  })

  it('disables the select while repos load but keeps a persisted repo selected', () => {
    window.localStorage.setItem('ccswitch.lastRepo', 'acme/web')
    render(<DispatchBar {...makeProps({ repos: [], reposLoading: true })} />)
    const select = screen.getByRole('combobox', { name: /repository/i })
    expect(select).toBeDisabled()
    expect(select).toHaveValue('acme/web')
  })
})
```

2. Run — fails (module missing).

3. Implement `web/src/components/DispatchBar.tsx`. Sketch (exact class strings mirror existing
   components — light + `dark:` pairs, `min-h-[44px]`, `focus-visible:ring-blue-500`):

```tsx
import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import type { GithubRepo } from '@/api/hooks'
import { getLastRepo, setLastRepo } from '@/lib/lastRepo'

export function caretLineEdges(value: string, caret: number) {
  const firstNewline = value.indexOf('\n')
  const atFirstLine = firstNewline === -1 || caret <= firstNewline
  const lastNewline = value.lastIndexOf('\n')
  const atLastLine = lastNewline === -1 || caret > lastNewline
  return { atFirstLine, atLastLine }
}

export interface DispatchBarProps { /* …as in Interfaces above… */ }

export function DispatchBar(props: DispatchBarProps) {
  const { repos, reposLoading, reposError, onDispatch, onNavigate, onOpenHighlighted,
    onRowAction, onShowHelp, onEscape, activeDescendantId, autoFocusKey, isPending } = props
  const [text, setText] = useState('')
  const [repo, setRepo] = useState<string>(() => getLastRepo() ?? '')
  const ref = useRef<HTMLTextAreaElement>(null)

  useEffect(() => { ref.current?.focus() }, [autoFocusKey])

  function dispatchNow() {
    const trimmed = text.trim()
    if (!trimmed) { onOpenHighlighted(); return }
    onDispatch({ initialPrompt: trimmed, repo: repo || null })
    setText('')
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    const el = e.currentTarget
    const caret = el.selectionStart ?? 0
    const empty = text === ''
    if (e.altKey && /^[1-9]$/.test(e.key)) {
      e.preventDefault(); onRowAction({ type: 'jump', index: Number(e.key) - 1 }); return
    }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); dispatchNow(); return }
    if (e.key === 'Escape') { e.preventDefault(); setText(''); onEscape(); return }
    if (e.key === '?') { e.preventDefault(); onShowHelp(); return }
    if (e.key === 'ArrowUp' && (empty || caretLineEdges(text, caret).atFirstLine)) {
      e.preventDefault(); onNavigate('up'); return
    }
    if (e.key === 'ArrowDown' && (empty || caretLineEdges(text, caret).atLastLine)) {
      e.preventDefault(); onNavigate('down'); return
    }
    if (e.key === 'ArrowRight' && caret >= text.length) {
      e.preventDefault(); onOpenHighlighted(); return
    }
    if (empty && (e.key === 'r' || e.key === 'F2')) {
      e.preventDefault(); onRowAction({ type: 'rename' }); return
    }
    if (empty && e.key === 'Delete') {
      e.preventDefault(); onRowAction({ type: 'delete' }); return
    }
  }

  function handleRepoChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const v = e.target.value
    setRepo(v)
    if (v) setLastRepo(v)
  }

  // …textarea (aria-label "New session prompt", aria-activedescendant, aria-controls="session-listbox")
  //   + repo <select> (aria-label "Repository", loading/error option text mirroring NewSessionDialog)…
}
```

4. Run — passes.

---

## Task 3 — Session-list selection state + highlight + `↑/↓/Enter/→` nav wired Dashboard → SessionList → SessionCard

**Files**
- `web/src/components/SessionList.tsx` (edit — add selection props + `role="listbox"`)
- `web/src/components/SessionCard.tsx` (edit — `selected` prop, `role="option"`, `aria-selected`,
  selection style, scroll-into-view)
- `web/src/components/__tests__/SessionList.test.tsx` (edit — add selection tests)

**Interfaces**
- `SessionList` new props (additive — keep all existing):

```ts
interface SessionListProps {
  /* …existing… */
  selectedId?: string | null
}
```

- `SessionCard` new props:

```ts
interface SessionCardProps {
  /* …existing… */
  selected?: boolean
}
```

- The `<ul>` gains `id="session-listbox"`, `role="listbox"`, `aria-label="Sessions"`,
  `aria-activedescendant={selectedId ? 'row-'+selectedId : undefined}`. Each row's existing open
  button (`id="row-<id>"`, `SessionCard.tsx:59-69`) gains `role="option"` and
  `aria-selected={selected}`.

**Behaviors**
- When `selected` is true, `SessionCard` adds a visible ring/background
  (`ring-2 ring-blue-500 bg-blue-50 dark:bg-blue-950/30`) and, in a `useEffect` keyed on
  `selected`, calls `ref.current?.scrollIntoView({ block: 'nearest' })`.
- Roving focus: the selected option gets `tabIndex={0}`, others `tabIndex={-1}` — but DOM focus
  stays in the dispatch textarea, so this is for completeness; `aria-activedescendant` drives AT.

**TDD steps**

1. Add to `SessionList.test.tsx`:

```tsx
it('marks the selected session row with aria-selected', () => {
  render(
    <SessionList
      sessions={SESSIONS}
      accounts={ACCOUNTS}
      selectedId="sess-2"
      onNew={onNew} onOpen={onOpen} onRename={onRename} onClose={onClose}
    />,
    { wrapper },
  )
  const selected = screen.getByRole('option', { name: /open session research/i })
  expect(selected).toHaveAttribute('aria-selected', 'true')
  const other = screen.getByRole('option', { name: /open session work session/i })
  expect(other).toHaveAttribute('aria-selected', 'false')
})

it('exposes the list as a listbox with aria-activedescendant', () => {
  render(
    <SessionList
      sessions={SESSIONS} accounts={ACCOUNTS} selectedId="sess-1"
      onNew={onNew} onOpen={onOpen} onRename={onRename} onClose={onClose}
    />,
    { wrapper },
  )
  const listbox = screen.getByRole('listbox', { name: /sessions/i })
  expect(listbox).toHaveAttribute('aria-activedescendant', 'row-sess-1')
})
```

   (The existing "calls onOpen…" tests still pass because the open control is still a focusable
   element with the same accessible name; update `getByRole('button', …)` → `getByRole('option', …)`
   in those two existing tests since the role changes.)

2. Run — fails.

3. Edit `SessionList.tsx`: add `selectedId`, switch `<ul>` to listbox semantics, pass
   `selected={session.id === selectedId}` to each `SessionCard`. Edit `SessionCard.tsx`: accept
   `selected`, add `role="option"` + `aria-selected` to the open button, conditional ring/bg,
   `tabIndex`, and the `scrollIntoView` effect.

4. Run — passes (including updated existing tests).

---

## Task 4 — Row actions (`r`/`F2` rename, `Delete`, `Alt+1`–`9`) resolved in Dashboard

**Files**
- `web/src/components/Dashboard.tsx` (edit — selection state + action handlers)
- `web/src/components/__tests__/KeyboardSessions.test.tsx` (new — Dashboard-level integration)

**Interfaces**
- `Dashboard` gains:

```ts
const [highlightedIndex, setHighlightedIndex] = useState(-1)
const selectedSession = highlightedIndex >= 0 ? sessions[highlightedIndex] : undefined
const selectedId = selectedSession?.id ?? null
```

- Handlers passed to `DispatchBar`:
  - `onNavigate(dir)`: clamp move. `up` → `Math.max(0, idx-1)` (from `-1` go to `0` only if
    `sessions.length`); `down` → `Math.min(sessions.length-1, idx+1)`; no-op when list empty.
  - `onOpenHighlighted()`: `if (selectedSession) handleOpen(selectedSession.id)`.
  - `onRowAction({type})`:
    - `rename` → `if (selectedSession) handleRename(selectedSession.id)` (opens existing
      `RenameDialog`).
    - `delete` → `if (selectedSession) handleClose(selectedSession.id)` (the existing
      `useCloseSession` path — confirmed reuse).
    - `jump` → `const s = sessions[index]; if (s) { setHighlightedIndex(index); handleOpen(s.id) }`.
  - `onEscape()`: `setHighlightedIndex(-1)`.
- Clamp effect:

```ts
useEffect(() => {
  setHighlightedIndex((i) => (sessions.length === 0 ? -1 : Math.min(i, sessions.length - 1)))
}, [sessions.length])
```

- `SessionList` receives `selectedId={selectedId}`.

**TDD steps**

1. Write `KeyboardSessions.test.tsx` — same `vi.mock('@/api/hooks', …)` scaffold +
   `setupDefaultMocks` as `DashboardTabs.test.tsx:9-79`. Provide 3 sessions and a spy
   `onOpenSession` via `render(<Dashboard onOpenSession={spy} />)`. The dispatch textarea is the
   focused element on the Sessions tab.

```tsx
it('ArrowDown highlights the first session, ArrowDown again the second', async () => {
  const user = userEvent.setup()
  vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
  render(<Dashboard />, { wrapper })
  const box = screen.getByRole('textbox', { name: /new session prompt/i })
  await user.click(box)
  await user.keyboard('{ArrowDown}')
  expect(screen.getByRole('option', { name: /open session one/i })).toHaveAttribute('aria-selected', 'true')
  await user.keyboard('{ArrowDown}')
  expect(screen.getByRole('option', { name: /open session two/i })).toHaveAttribute('aria-selected', 'true')
})

it('Enter on empty prompt opens the highlighted session', async () => {
  const user = userEvent.setup()
  const onOpen = vi.fn()
  vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
  render(<Dashboard onOpenSession={onOpen} />, { wrapper })
  await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
  await user.keyboard('{ArrowDown}{Enter}')
  expect(onOpen).toHaveBeenCalledWith('s1')
})

it('r on empty prompt opens the rename dialog for the highlighted session', async () => {
  const user = userEvent.setup()
  vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
  render(<Dashboard />, { wrapper })
  await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
  await user.keyboard('{ArrowDown}r')
  expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
})

it('Delete on empty prompt calls the close (delete) hook for the highlighted session', async () => {
  const user = userEvent.setup()
  const closeMutate = vi.fn()
  vi.mocked(hooks.useCloseSession).mockReturnValue({ mutate: closeMutate, isPending: false } as any)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
  render(<Dashboard />, { wrapper })
  await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
  await user.keyboard('{ArrowDown}{Delete}')
  expect(closeMutate).toHaveBeenCalledWith('s1')
})

it('Alt+3 opens the third session directly', async () => {
  const user = userEvent.setup()
  const onOpen = vi.fn()
  vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
  render(<Dashboard onOpenSession={onOpen} />, { wrapper })
  await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
  await user.keyboard('{Alt>}3{/Alt}')
  expect(onOpen).toHaveBeenCalledWith('s3')
})

it('nav is a no-op when the session list is empty', async () => {
  const user = userEvent.setup()
  const onOpen = vi.fn()
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as any)
  render(<Dashboard onOpenSession={onOpen} />, { wrapper })
  await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
  await user.keyboard('{ArrowDown}{Enter}')
  expect(onOpen).not.toHaveBeenCalled()
})
```

   (`THREE` = three `makeSession` objects with ids `s1/s2/s3` and names `One/Two/Three`. Mocks
   for `useGithubRepos` already return `{ data: [], isLoading: false, isError: false }`.)

2. Run — fails.

3. Edit `Dashboard.tsx`: add the state/handlers/effect, render `DispatchBar` inside the Sessions
   tabpanel below `SessionList`, pass `selectedId`.

4. Run — passes.

---

## Task 5 — `?` shortcuts help overlay (modal, focus trap, Esc closes)

**Files**
- `web/src/components/ShortcutsHelp.tsx` (new)
- `web/src/components/__tests__/ShortcutsHelp.test.tsx` (new)

**Interfaces**
- Consumes: `{ onClose: () => void }`.
- Produces: a `role="dialog"` `aria-modal="true"` `aria-labelledby="shortcuts-help-title"` overlay
  listing every binding (Enter dispatch / Shift+Enter newline / ↑↓ navigate / →/Enter-empty open /
  Esc clear / r·F2 rename / Delete delete / Alt+1–9 jump / ? help / in-session Esc back /
  Shift+Tab cycle mode). Mirrors the dialog scaffold from `DeleteAccountDialog`
  (`Dashboard.tsx:415-499`): backdrop click + `Escape` + `Tab` focus cycling; focuses the close
  button on mount.

**TDD steps**

1. Write `ShortcutsHelp.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ShortcutsHelp } from '../ShortcutsHelp'

describe('ShortcutsHelp', () => {
  it('renders as a labelled modal dialog', () => {
    render(<ShortcutsHelp onClose={vi.fn()} />)
    const dialog = screen.getByRole('dialog', { name: /keyboard shortcuts/i })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
  })

  it('lists core bindings', () => {
    render(<ShortcutsHelp onClose={vi.fn()} />)
    expect(screen.getByText(/navigate sessions/i)).toBeInTheDocument()
    expect(screen.getByText(/rename/i)).toBeInTheDocument()
    expect(screen.getByText(/cycle permission mode/i)).toBeInTheDocument()
  })

  it('Escape closes', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<ShortcutsHelp onClose={onClose} />)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalled()
  })

  it('backdrop click closes', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<ShortcutsHelp onClose={onClose} />)
    await user.click(screen.getByRole('dialog'))
    expect(onClose).toHaveBeenCalled()
  })

  it('focuses the close button on mount', () => {
    render(<ShortcutsHelp onClose={vi.fn()} />)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /close/i }))
  })
})
```

2. Run — fails.

3. Implement `ShortcutsHelp.tsx` (copy the `DeleteAccountDialog` keydown/focus-trap effect verbatim,
   swap content for a `<dl>`/table of bindings).

4. Run — passes. Wire it in Dashboard: `const [showHelp, setShowHelp] = useState(false)`;
   `onShowHelp={() => setShowHelp(true)}`; render `{showHelp && <ShortcutsHelp onClose={() => setShowHelp(false)} />}`.

---

## Task 6 — Integrate DispatchBar create-and-open + auto-focus on dashboard return

**Files**
- `web/src/components/Dashboard.tsx` (edit — `onDispatch` wiring, `autoFocusKey`)
- `web/src/App.tsx` (edit — pass a focus signal so DispatchBar re-focuses after Back)
- `web/src/components/__tests__/KeyboardSessions.test.tsx` (edit — add dispatch+create tests)
- `web/src/App.test.tsx` (edit — add a return-focus assertion)

**Interfaces**
- `Dashboard` `onDispatch`:

```ts
function handleDispatch({ initialPrompt, repo }: { initialPrompt: string; repo: string | null }) {
  const repoInfo = repo ? repos.find((r) => r.nameWithOwner === repo) : undefined
  createSession.mutate(
    { initialPrompt, repo: repo ?? undefined, branch: repoInfo?.defaultBranch ?? undefined },
    {
      onSuccess: (session) => {
        if (repo) setLastRepo(repo)          // persist last-chosen repo on success
        onOpenSession?.(session.id)          // open the new session
      },
    },
  )
}
```

   (`repos` from `useGithubRepos()` is now called in Dashboard — add the hook there and pass
   `repos/reposLoading/reposError` to `DispatchBar`. `branch` defaults to the repo's
   `defaultBranch`; the server falls back to the repo default when `branch` is omitted, so the
   `?? undefined` is safe.)

- `DashboardProps` gains `focusDispatchSignal?: unknown` (a counter/timestamp). Dashboard passes
  it through as `autoFocusKey` to `DispatchBar`. On first mount DispatchBar auto-focuses anyway.

- `App.tsx`: bump a `dashboardFocusNonce` (number) in `handleBack` and pass it to
  `<Dashboard focusDispatchSignal={dashboardFocusNonce} … />`. This re-focuses the prompt when the
  user returns from a session (the existing `requestAnimationFrame` row-focus restore in
  `handleBack` `:38-40` is for the keyboard-mouse path; for the keyboard-first flow we want the
  prompt focused — choose one: keep row focus only when `originElementId` exists AND the prompt
  isn't the intended target; simplest is to focus the prompt and drop row-restore. Decision: focus
  the prompt — it is the always-on control in this feature. Update the App test accordingly).

**TDD steps**

1. Add to `KeyboardSessions.test.tsx`:

```tsx
it('Enter with text creates a session with the prompt + repo default branch and opens it', async () => {
  const user = userEvent.setup()
  const createMutate = vi.fn((_input, opts) => opts?.onSuccess?.({ id: 'new-1' }))
  const onOpen = vi.fn()
  vi.mocked(hooks.useCreateSession).mockReturnValue({ mutate: createMutate, isPending: false } as any)
  vi.mocked(hooks.useGithubRepos).mockReturnValue({
    data: [{ nameWithOwner: 'acme/web', name: 'web', owner: 'acme', isPrivate: false, url: '', defaultBranch: 'main', pushedAt: null, cloned: true }],
    isLoading: false, isError: false,
  } as any)
  window.localStorage.setItem('ccswitch.lastRepo', 'acme/web')
  render(<Dashboard onOpenSession={onOpen} />, { wrapper })
  const box = screen.getByRole('textbox', { name: /new session prompt/i })
  await user.click(box)
  await user.type(box, 'add a button')
  await user.keyboard('{Enter}')
  expect(createMutate).toHaveBeenCalledWith(
    { initialPrompt: 'add a button', repo: 'acme/web', branch: 'main' },
    expect.anything(),
  )
  expect(onOpen).toHaveBeenCalledWith('new-1')
})

it('dispatch with no repo selected omits repo and branch', async () => {
  const user = userEvent.setup()
  const createMutate = vi.fn((_i, opts) => opts?.onSuccess?.({ id: 'n2' }))
  vi.mocked(hooks.useCreateSession).mockReturnValue({ mutate: createMutate, isPending: false } as any)
  render(<Dashboard onOpenSession={vi.fn()} />, { wrapper })
  const box = screen.getByRole('textbox', { name: /new session prompt/i })
  await user.click(box)
  await user.type(box, 'hello')
  await user.keyboard('{Enter}')
  expect(createMutate).toHaveBeenCalledWith(
    { initialPrompt: 'hello', repo: undefined, branch: undefined },
    expect.anything(),
  )
})
```

2. Add to `App.test.tsx`: open a session, press Back, assert the dispatch prompt is focused:

```tsx
it('focuses the dispatch prompt after returning from a session', async () => {
  const user = userEvent.setup()
  render(<App />)
  await waitFor(() => screen.getByRole('button', { name: /open session work/i }))
  await user.click(screen.getByRole('button', { name: /open session work/i }))
  await user.click(await screen.findByRole('button', { name: /back to session list/i }))
  await waitFor(() =>
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: /new session prompt/i })),
  )
})
```

3. Run — fails.

4. Implement: add `useGithubRepos()` in Dashboard, `handleDispatch`, render
   `<DispatchBar … autoFocusKey={focusDispatchSignal} onDispatch={handleDispatch} … />`. In
   `App.tsx`, add the nonce + pass it; change `handleBack` to focus the prompt (drop or keep the
   row-restore per the Decision above).

5. Run — passes.

---

## Task 7 — In-session shortcuts: `Esc` → back, `Shift+Tab` → cycle permission mode

**Files**
- `web/src/components/session/SessionView.tsx` (edit — global keydown for Esc + Shift+Tab)
- `web/src/components/session/__tests__/SessionView.keyboard.test.tsx` (new)

**Interfaces**
- `SessionView` adds a `useEffect` window `keydown` listener:
  - `Escape` (when no dialog/composer-specific handler consumes it) → `handleBack()`.
    Guard: ignore if the event target is the composer textarea **with non-empty value** so Esc in
    the composer doesn't yank the user out mid-draft. Simplest robust rule: `Esc` triggers Back
    only when `document.activeElement` is **not** the composer textarea, OR the textarea is empty.
  - `Shift+Tab` → `e.preventDefault()` + cycle: compute next mode from `PERMISSION_MODES`
    (`web/src/session/permissionModes.ts`) given current `mode`, call `handleSetMode(next)`.

```ts
import { PERMISSION_MODES } from '@/session/permissionModes'

function nextMode(current: string): string {
  const values = PERMISSION_MODES.map((m) => m.value)
  const i = values.indexOf(current)
  return values[(i + 1) % values.length]
}
```

   Cycle handler:

```ts
useEffect(() => {
  function onKey(e: KeyboardEvent) {
    if (e.key === 'Tab' && e.shiftKey) {
      e.preventDefault()
      handleSetMode(nextMode(modeRef.current))
    } else if (e.key === 'Escape') {
      const ae = document.activeElement as HTMLElement | null
      const inComposer = ae?.getAttribute('aria-label') === 'Message'
      if (!inComposer || (ae as HTMLTextAreaElement)?.value === '') handleBack()
    }
  }
  window.addEventListener('keydown', onKey)
  return () => window.removeEventListener('keydown', onKey)
}, [handleSetMode, handleBack])
```

   (`mode` is read via a `modeRef` updated each render to avoid re-binding the listener; or include
   `mode` in deps — either is fine, pick the ref to keep the listener stable.)

**TDD steps**

1. Write `SessionView.keyboard.test.tsx`. Mock `@/ws/client`'s `useSessionSocket` like
   `App.test.tsx:15-35` (return `attach/detach/on/off/setPermissionMode/...` vi.fns,
   `connectionState: 'connected'`). Render `<SessionView sessionId="s1" onBack={onBack} />`.

```tsx
it('Escape calls onBack when not typing in the composer', async () => {
  const user = userEvent.setup()
  const onBack = vi.fn()
  render(<SessionView sessionId="s1" onBack={onBack} />)
  await user.keyboard('{Escape}')
  expect(onBack).toHaveBeenCalled()
})

it('Escape does NOT exit when the composer has text', async () => {
  const user = userEvent.setup()
  const onBack = vi.fn()
  render(<SessionView sessionId="s1" onBack={onBack} />)
  const box = screen.getByRole('textbox', { name: /message/i })
  await user.click(box)
  await user.type(box, 'wip')
  await user.keyboard('{Escape}')
  expect(onBack).not.toHaveBeenCalled()
})

it('Shift+Tab cycles the permission mode via setPermissionMode', async () => {
  const user = userEvent.setup()
  const setPermissionMode = vi.fn()
  // override the ws mock so this spy is observed (see setup note below)
  render(<SessionView sessionId="s1" onBack={vi.fn()} />)
  await user.keyboard('{Shift>}{Tab}{/Shift}')
  expect(setPermissionMode).toHaveBeenCalledWith('s1', 'auto') // interactive → auto
})
```

   (For the `setPermissionMode` spy: define the vi.fn() in the `vi.mock('@/ws/client', …)` factory
   scope and return it from `useSessionSocket`, mirroring `App.test.tsx`. Initial `mode` is
   `'interactive'` (`SessionView.tsx:51`), so the first cycle → `'auto'`.)

2. Run — fails.

3. Edit `SessionView.tsx`: add the `nextMode` helper + the `useEffect` keydown listener +
   `modeRef`.

4. Run — passes. Also run the full web suite (`cd web && pnpm test`) + `pnpm typecheck` +
   `pnpm lint` to confirm no regressions in `App.test.tsx`, `DashboardTabs.test.tsx`,
   `SessionList.test.tsx`.

---

## Verification (end of plan)

```bash
cd web
pnpm test          # vitest run — all suites green
pnpm typecheck     # tsc --noEmit
pnpm lint          # eslint --max-warnings 0
```

Manual smoke (optional, `pnpm dev`): dashboard prompt is focused on load; type → Enter creates +
opens; ↑/↓ move the highlight while the prompt stays focused; `?` shows the cheat sheet; in a
session, `Esc` returns and `Shift+Tab` rotates the mode pill.
