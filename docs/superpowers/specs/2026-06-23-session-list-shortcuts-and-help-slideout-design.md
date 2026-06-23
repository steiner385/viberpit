# Session-list keyboard shortcuts + global help slide-out

**Date:** 2026-06-23
**Status:** Approved — ready for implementation plan

## Problem

The web UI already has a keyboard layer for the sessions list (`↑/↓` virtual-focus
navigation, `Enter`/`→` to open, `Ctrl+R` rename, `Ctrl+X` archive, `Alt+1-9` jump),
and a `?`-triggered **modal** (`ShortcutsHelp`) that lists shortcuts. Two gaps:

1. Common session actions still require the mouse — there's no fast path to
   quick-continue or permanently delete the highlighted session, and the existing
   action bindings are an ad-hoc pair (`Ctrl+R`, `Ctrl+X`) rather than a coherent set.
2. The help surface is a shortcuts-only modal. New and returning users have no single
   place to learn navigation across the whole app or core concepts.

## Constraints (from existing code)

- **Selection is "virtual."** Real DOM focus stays in the `DispatchBar` textarea while
  the user navigates; the highlighted session is tracked by `highlightedIndex` in
  `Dashboard.tsx` and surfaced via `aria-activedescendant`. Plain single-letter keys
  therefore collide with typing — **all new action bindings must be modifier-guarded.**
- **Dual rendering.** The sessions list renders in two places (mobile inline in
  `Dashboard.tsx`, desktop master-detail in `DesktopShell.tsx`). Shortcut logic must be
  shared, not duplicated.
- **Drift risk already present.** `ShortcutsHelp.tsx` (lines 14-33) hardcodes the
  shortcut list independently of the real handler in `DispatchBar.tsx`. This is the
  "N parallel implementations of one concept" anti-pattern; the rationalization fixes it.
- **Custom overlays only.** No shadcn Sheet/Drawer exists; all dialogs are hand-built
  fixed overlays (`role="dialog"`, `aria-modal`, Esc-to-close, focus trap). The
  slide-out follows that pattern — no new dependency.

## Decisions

- **Modifier family:** `Alt + key`, scheme-wide. Rationale: `Ctrl+letter` collides with
  native textarea editing (`Ctrl+A/C/X/Z` select-all/copy/cut/undo, `Ctrl+Backspace`/
  `Ctrl+Delete` word-delete), forcing an ugly "only when empty" gate. `Alt+letter` is not
  a text-editing command, so the bindings fire safely **whether or not a prompt is
  drafted** — and they join the existing `Alt+1-9` "jump to session" family for one
  consistent scheme.
- **Rationalize, don't just add:** define one coherent action set; the existing
  `Ctrl+R` (rename) and `Ctrl+X` (archive) are **removed** and re-homed on `Alt`.
- **Replace** the shortcuts modal with a slide-out; delete the modal in the same PR.

## Design

### 1. Shortcut scheme

| Action | Binding | Behavior |
|---|---|---|
| Open / enter session | `Enter` / `→` (empty prompt) | unchanged |
| Jump to session N | `Alt+1`–`Alt+9` | unchanged |
| Quick-continue | `Alt+C` | sends `continue` to the highlighted session |
| Rename | `Alt+R` | replaces `Ctrl+R` |
| Archive (soft) | `Alt+A` | replaces `Ctrl+X`; keeps the existing press-twice 2s guard (`pendingDelete`) |
| Delete (permanent) | `Alt+D` | opens the existing `PermanentDeleteDialog` — never instant |

- All actions target the **highlighted** session and fire only in the live sessions-list
  context (Dashboard sessions tab / DesktopShell list).
- Because every binding is `Alt`-modified and `Alt+letter` performs no textarea editing,
  they fire whether or not a prompt is drafted — no typing collision, no clobbered caret.
- `Ctrl+R` and `Ctrl+X` are **removed** so each action has a single home.
- `↑/↓` navigation and `Esc`/`?` behavior are unchanged.

**Implementation caveat:** `Alt+D` focuses the browser address bar on Chrome/Firefox
(Win/Linux), and `Option+letter` types special chars on macOS. Both are suppressed by
`preventDefault()` in the keydown handler — the same mechanism the existing `Alt+1-9`
already relies on. The handler must call `preventDefault()` whenever it matches an `Alt`
binding.

### 2. Single source of truth — shortcuts registry

Introduce one registry module, e.g. `web/src/lib/shortcuts.ts`. Each entry:

```ts
type ShortcutGroup = 'sessions-list' | 'in-session' | 'dialogs' | 'palette' | 'navigation'
interface Shortcut {
  id: string            // stable id, e.g. 'session.quick-continue'
  keys: string          // canonical combo, e.g. 'Ctrl+Enter' (also used to render <kbd>)
  label: string         // human description for the help panel
  group: ShortcutGroup
  match: (e: KeyboardEvent) => boolean  // single matcher used by the handler
}
```

Two consumers, one definition:

- **Handler** — matching lives in `DispatchBar.handleKeyDown`, which is already the single
  shared component rendered by **both** `Dashboard.tsx` and `DesktopShell.tsx` (so the two
  renderings stay in lockstep automatically — no second handler to keep in sync). The
  handler iterates the registry's wired (`match`-bearing) `sessions-list` entries, and on a
  match calls `preventDefault()` and dispatches the entry's `action` through the existing
  `onRowAction` prop. The old hardcoded `Ctrl+R`/`Ctrl+X` branches are deleted; only the
  registry path remains. (We deliberately do *not* add a second global keydown listener
  beside `useGlobalListNav` — focus lives in the textarea, so a global listener would risk
  double-handling.)
- **Help slide-out** — renders directly from the registry, grouped by `group`.

Only the four `Alt` actions carry a `match`/`action` (they are statically matchable);
`↑/↓`, `Enter`/`→`, `Esc`, `?`, and `Alt+1-9` keep their existing bespoke handling in
`DispatchBar` (they depend on caret position / index) and appear in the registry as
**display-only** entries so the help panel documents them. Adding a wired binding once
makes it both functional and documented; the help panel can no longer drift from the
wired handler.

**`onRowAction` union change:** the existing union
`{type:'rename'} | {type:'delete'} | {type:'jump';index}` becomes
`{type:'rename'} | {type:'archive'} | {type:'permanent-delete'} | {type:'continue'} | {type:'jump';index}`
(the old `'delete'` — which performs *archive* via `useCloseSession` — is renamed to
`'archive'` for clarity). This is changed in the `DispatchBar` prop type and both
`handleRowAction` implementations (Dashboard + DesktopShell).

### 3. Help slide-out

- New reusable overlay primitive (e.g. `web/src/components/ui/Sheet.tsx` or a focused
  `HelpDrawer`) sliding from the right, built on the codebase's existing custom-overlay
  pattern: `role="dialog"`, `aria-modal`, Esc-to-close, focus trap (reuse the trap already
  in `ShortcutsHelp.tsx`). No new dependency.
- **`ShortcutsHelp` modal is deleted** in the same PR; its content moves into the slide-out.
- **Contents**, three sections:
  1. **All keyboard shortcuts**, grouped by context (sessions-list, in-session, dialogs,
     palette) — rendered from the registry.
  2. **Navigation map** — main views (Sessions / Accounts / Archived / Session / Review)
     and how to move between them.
  3. **Quick tips / concepts** — account = config dir, archive vs delete, quick-continue,
     permission modes.
- **Triggers (both):**
  - Global `?` key — works anywhere (not just an empty dispatch bar), guarded to skip when
    typing in an input/textarea or when another dialog is open.
  - A visible `?` icon button in the app chrome/header for mouse and touch users (the app
    is mobile-accessible).

## Testing (vitest)

- Handler matches each wired `Alt` binding to the right `onRowAction` type and calls
  `preventDefault()`; the same letters **without** `Alt` type normally into the textarea.
- `Alt+D` opens `PermanentDeleteDialog` rather than deleting immediately.
- `Alt+A` archives with the press-twice guard intact; `Ctrl+R`/`Ctrl+X` no longer fire.
- `Alt+C` sends `continue` to the highlighted session (`useSendPrompt`).
- Help slide-out opens via both `?` and the button, traps focus, closes on Esc.
- Help renders an entry for **every** registry shortcut (drift guard).
- Both `Dashboard` and `DesktopShell` route through the shared `DispatchBar` handler
  (no dual matching path).

## Non-goals (YAGNI)

- No action shortcuts in the Archived/Accounts lists yet (help still documents how to
  navigate there).
- No user-remappable keybindings.
- No context-aware "active right now" highlighting in the help panel (possible later).
- No user-configurable / platform-adaptive (Cmd) bindings in v1.
- The non-`Alt` bespoke shortcuts (`↑/↓`, `Enter`/`→`, `Esc`, `Alt+1-9`) are not refactored
  to carry registry `match` functions — they stay in `DispatchBar` and appear in the help
  panel as display-only entries.
