# Session-list keyboard shortcuts + global help slide-out

**Date:** 2026-06-23
**Status:** Approved — ready for implementation plan

## Problem

The web UI already has a keyboard layer for the sessions list (`↑/↓` virtual-focus
navigation, `Enter`/`→` to open, `Ctrl+R` rename, `Ctrl+X` archive, `Alt+1-9` jump),
and a `?`-triggered **modal** (`ShortcutsHelp`) that lists shortcuts. Two gaps:

1. Common session actions (continue, delete) have no fast keyboard path, and the existing
   action bindings (`Ctrl+R`, `Ctrl+X`) collide with native browser/editing keys.
2. The help surface is a shortcuts-only modal. Users have no single place to learn
   navigation across the whole app or core concepts.

## Constraints (from existing code)

- **Selection is "virtual" today.** Real DOM focus stays in the `DispatchBar` textarea
  while the user navigates; the highlighted session is tracked by `highlightedIndex`
  (Dashboard.tsx ~1336, DesktopShell.tsx) and surfaced via `aria-activedescendant`.
- **Roving-focus scaffolding already exists.** `SessionCard` has `tabIndex={selected ? 0 : -1}`
  and an Enter/Space keydown handler (lines 57-62, 99-100); `SessionList` tracks
  `aria-activedescendant`. The markup is ready for the highlighted row to take *real* focus.
- **`SessionCard` is the shared atom** rendered by both `Dashboard` and `DesktopShell`, so a
  keydown handler placed there serves both layouts (same DRY property `DispatchBar` has).
- **Drift risk already present.** `ShortcutsHelp.tsx` (lines 14-33) hardcodes the shortcut
  list independently of the real handler — the "N parallel implementations" anti-pattern.
- **Custom overlays only.** No shadcn Sheet/Drawer; dialogs are hand-built fixed overlays
  (`role="dialog"`, `aria-modal`, Esc-to-close, focus trap). The slide-out follows suit.

## Why a list-focus mode (the key decision)

Every single-modifier binding collides with something: `Ctrl+letter` is editing/browser
(copy, cut, reload, bookmark), and `Alt+letter` isn't safe either (`Alt+D` = address bar,
`Alt+←/→` = back/forward, Windows menu key). The only way to get clean, mnemonic
**single-key** shortcuts is to make them active in a context where keystrokes are *not*
text input. So we introduce an explicit **list-focus mode**: the highlighted row takes real
DOM focus (a `<div>`, not the textarea), where single letters can't type and are therefore
free to be commands.

## Decisions

- **Two modes:**
  - **Compose** (default) — focus in the `DispatchBar` textarea. Unchanged: type a prompt,
    `↑/↓` move the highlight (virtual), `Enter` creates/opens, `Esc` clears, `?` opens help,
    `Alt+1-9` jumps.
  - **List-focus** — the highlighted `SessionCard` has real DOM focus; single-key shortcuts
    act on it.
- **Enter list mode:** `Tab` in the textarea **when the prompt is empty** (preventDefault →
  focus the highlighted row, or the first row if none). With a draft typed, `Tab` keeps its
  native job (reach the Create button). Clicking a row also enters list mode.
- **Exit to compose:** `Esc`, `←`, or `Shift+Tab` → refocus the textarea (via the existing
  `autoFocusKey` mechanism).
- **`Ctrl+R` and `Ctrl+X` are removed** — their actions move to single keys in list mode.
- **Replace** the shortcuts modal with a slide-out; delete the modal in the same PR.

## Design

### 1. Keys

**Compose mode (focus in textarea) — unchanged:**

| Key | Action |
|---|---|
| `↑` / `↓` | Move highlight (at line boundary / empty) |
| `Enter` (with text) | Create session from prompt |
| `Enter` / `→` (empty) | Open highlighted session |
| `Shift+Enter` | Newline |
| `Esc` | Clear prompt + deselect |
| `Alt+1`–`9` | Jump to session N |
| `?` (empty) | Open help slide-out |
| **`Tab` (empty)** | **Enter list-focus mode on the highlighted row** |

**List-focus mode (highlighted row has real focus) — new:**

| Key | Action |
|---|---|
| `↑` / `↓` | Move focus to prev / next row |
| `Enter` / `→` / `Space` | Open the focused session |
| `c` | Continue (resume — sends `continue`) |
| `r` | Rename |
| `a` or `Backspace` | Archive |
| `d` or `Delete` | Delete permanently (opens confirm dialog) |
| `Esc` / `←` / `Shift+Tab` | Return to compose (focus the prompt) |

- Single keys are safe because focus is on a non-input element — no typing collision, no
  browser conflict.
- **Archive is single-press** in list mode (it's reversible via unarchive, and entering list
  mode + pressing the key is already deliberate). The press-twice guard used for the old
  `Ctrl+X` is dropped. **Delete always routes through `PermanentDeleteDialog`** — never
  instant.
- `highlightedIndex` remains the single "which row" state, shared by both modes; in list
  mode the focused row follows it.

### 2. Architecture — registry + where handling lives

Introduce one registry module, `web/src/lib/shortcuts.ts`, as the single source of truth for
both matching and help display:

```ts
export type ListRowAction =
  | { type: 'open' }
  | { type: 'continue' }
  | { type: 'rename' }
  | { type: 'archive' }
  | { type: 'permanent-delete' }
  | { type: 'navigate'; dir: 'up' | 'down' }
  | { type: 'exit' }            // back to compose

export type KeyboardLike = Pick<KeyboardEvent, 'key' | 'code' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'>

export type ShortcutGroup = 'list-mode' | 'compose' | 'in-session'

export interface Shortcut {
  id: string
  keys: string                 // display string for the help panel
  description: string
  group: ShortcutGroup
  action?: ListRowAction        // present ⇒ wired into the SessionCard handler
  match?: (e: KeyboardLike) => boolean
}
```

- **List-mode matching lives in `SessionCard.onKeyDown`** — the shared atom. It runs the
  registry's `list-mode` matchers and invokes a single `onRowAction(action: ListRowAction)`
  callback prop. Because both shells render `SessionCard`, both get the behavior with no
  duplicated handler. (Plain keys like `c`/`r`/`a`/`d` match on `e.key`; `Backspace`/
  `Delete`/`Enter`/arrows match on `e.key` too — all unmodified, so no Mac remap issue.)
- **`SessionList`** threads `onRowAction` through to each `SessionCard`, and manages **focus**:
  when the app is in list mode and a card is the highlighted one, that card focuses itself
  (a `listMode` + `selected` prop drives a `useEffect` focus call; the existing
  scroll-into-view effect already fires on `selected`).
- **`DispatchBar`** gains a `Tab`-on-empty branch that calls a new `onEnterList()` prop; its
  old `Ctrl+R`/`Ctrl+X` branches are deleted. `↑/↓`, `Enter`, `Esc`, `?`, `Alt+digit` stay.
- **`Dashboard` / `DesktopShell`** own a `listMode` boolean. `onEnterList` sets it true (and
  ensures a row is highlighted); the `SessionCard` `exit` action sets it false and bumps
  `autoFocusKey`/`focusDispatchSignal` to pull focus back to the textarea. The `open`/
  `continue`/`rename`/`archive`/`permanent-delete`/`navigate` actions route to the existing
  handlers (`handleOpen`, `handleQuickResume`, `handleRename`, `handleClose`,
  `handleSessionDelete`/`setSessionDeleteTarget`, `handleNavigate`).
- **Help slide-out** renders directly from the registry, grouped by `group`.

Adding a wired binding once makes it both functional and documented; the help panel can no
longer drift from the handler.

### 3. Help slide-out

- New right-edge **slide-out** (`web/src/components/HelpDrawer.tsx`) built on the existing
  custom-overlay pattern (`role="dialog"`, `aria-modal`, Esc-to-close, focus trap reused
  from `ShortcutsHelp.tsx`). No new dependency.
- **`ShortcutsHelp` modal is deleted** in the same PR.
- **Contents**, three sections:
  1. **All keyboard shortcuts** — compose + list-mode + in-session, from the registry.
  2. **Navigation map** — main views (Sessions / Accounts / Archived / Session / Review) and
     how to move between them.
  3. **Quick tips / concepts** — account = config dir, list-focus mode (Tab in / Esc out),
     archive vs delete, quick-continue, permission modes.
- **Triggers (both):** global `?` (works anywhere except while typing in a field or with a
  dialog open) via a small `useGlobalHelpKey` hook, plus a visible `?` icon button in the
  app chrome.

## Testing (vitest)

- Registry: each `list-mode` matcher maps to the right `ListRowAction`; unmodified keys
  match (no Ctrl/Alt/Meta).
- `SessionCard`: in list mode, `c`/`r`/`a`/`Backspace`/`d`/`Delete`/`Enter`/`→`/`Space`/
  `↑`/`↓`/`Esc`/`←` each fire the right `onRowAction`; when a card is highlighted + listMode,
  it receives focus.
- `DispatchBar`: `Tab` on an empty prompt calls `onEnterList` (preventDefault); `Tab` with a
  draft does NOT (native focus move preserved); `Ctrl+R`/`Ctrl+X` no longer fire.
- Dashboard/DesktopShell integration: `Tab` enters list mode and focuses a row; `d`/`Delete`
  opens `PermanentDeleteDialog`; `c` sends `continue` (`useSendPrompt`); `a` archives
  (`useCloseSession`); `Esc` returns focus to the prompt.
- Help slide-out: opens via `?` and the button, traps focus, closes on Esc, renders an entry
  for **every** registry shortcut (drift guard).

## Non-goals (YAGNI)

- No action shortcuts in the Archived/Accounts lists yet (help documents navigation there).
- No user-remappable / platform-adaptive bindings.
- No context-aware "active right now" highlighting in the help panel.
- The press-twice archive guard is removed (replaced by deliberate list-mode), not reworked.
