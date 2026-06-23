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

- **Modifier family:** `Ctrl + key`, scheme-wide (same on all platforms; matches the two
  existing bindings). No platform-adaptive Cmd/Ctrl.
- **Rationalize, don't just add:** define one coherent action set; fold the existing
  `Ctrl+R`/`Ctrl+X` into it.
- **Replace** the shortcuts modal with a slide-out; delete the modal in the same PR.

## Design

### 1. Shortcut scheme

| Action | Binding | Behavior |
|---|---|---|
| Open / enter session | `Enter` / `→` (empty prompt) | unchanged |
| Quick-continue | `Ctrl+Enter` | sends `continue` to the highlighted session |
| Rename | `Ctrl+R` | unchanged |
| Archive (soft) | `Ctrl+Backspace` | replaces `Ctrl+X`; keeps the existing press-twice 2s guard (`pendingDelete`) |
| Delete (permanent) | `Ctrl+Delete` | opens the existing `PermanentDeleteDialog` — never instant |

- All actions target the **highlighted** session and fire only in the live sessions-list
  context (Dashboard sessions tab / DesktopShell list).
- Because every binding is `Ctrl`-modified, they fire whether or not a prompt is drafted
  — no typing collision.
- `Ctrl+X` is **removed** (folded into `Ctrl+Backspace`) so archive has a single home.
- `Alt+1-9` jump and `↑/↓` navigation are unchanged.

**Known caveat (accepted, not mitigated in v1):** on Mac laptops the `Delete` key is
Backspace; forward-delete is `Fn+Delete`. So `Ctrl+Delete` works on full keyboards but is
awkward on Mac laptops. No alias added — the help slide-out is the discovery path. Revisit
if it proves painful.

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

- **Handler** — a shared hook `useSessionListShortcuts` (sibling to the existing
  `useGlobalListNav`) iterates the `sessions-list` entries, runs `match`, and invokes the
  corresponding action. Guarded to no-op when a dialog is open or focus is in an unrelated
  input. Consumed by **both** `Dashboard.tsx` and `DesktopShell.tsx` so the two renderings
  stay in lockstep. The existing `DispatchBar` `Ctrl+R`/`Ctrl+X` handling is migrated onto
  the registry (no second matching path left behind).
- **Help slide-out** — renders directly from the registry, grouped by `group`.

Adding a binding once makes it both functional and documented; the help panel can no
longer drift from real behavior.

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

- Handler matches each `sessions-list` binding to the right action; modified combos fire
  while a prompt is drafted, and unmodified equivalents do **not** fire (they type).
- `Ctrl+Delete` opens `PermanentDeleteDialog` rather than deleting immediately.
- `Ctrl+Backspace` archives with the press-twice guard intact; `Ctrl+X` no longer archives.
- Help slide-out opens via both `?` and the button, traps focus, closes on Esc.
- Help renders an entry for **every** registry shortcut (drift guard).
- Both `Dashboard` and `DesktopShell` wire up `useSessionListShortcuts` (no dual handler).

## Non-goals (YAGNI)

- No action shortcuts in the Archived/Accounts lists yet (help still documents how to
  navigate there).
- No user-remappable keybindings.
- No context-aware "active right now" highlighting in the help panel (possible later).
- No platform-adaptive Cmd/Ctrl or Mac forward-delete alias in v1.
