# Agent View Keyboard Parity — Design (v2, against origin/master)

**Date:** 2026-06-22
**Base:** `origin/master` (HEAD `bcce9da`, PR #68). **This supersedes the v1 draft**,
which was written against a worktree 179 commits stale (pre-`DesktopShell`, pre-router).
**Scope:** Frontend-only (`web/`). Navigation gestures + rebind of two shortcuts in
both responsive layouts. No backend or data-model changes.

## Goal

Bring ccswitch's keyboard navigation to parity with Claude Code's **Agent View**
(`claude agents`), in **both** responsive layouts. Primary gap: you cannot press `←`
(Left Arrow) to detach from an open session back to the sessions list.

## Reference behavior (Claude Code Agent View) — validated, architecture-independent

Verified against the shipped Claude Code v2.1.185 binary + official docs
(`code.claude.com/docs/en/agent-view`):

- Depth-based master-detail: `↑`/`↓` move within the list; `→`/`Enter` **attach**;
  `←` **detach** back to the list.
- Conflict avoidance is an **empty-prompt gate**, not a modal nav state: `←` detaches
  only when the prompt is empty; with text it moves the cursor.
- `Ctrl+Z` is an **unconditional** force-detach escape hatch (for when a focused child
  widget swallows `←`).
- Detach is **non-destructive**: the session keeps running in the background.
- Returning selects the originating row.

## Real architecture (origin/master)

`App.tsx` is **responsive** (`useMediaQuery('(min-width: 1024px)')`):

- **Wide (≥1024px) → `DesktopShell`** — a two-pane master-detail, **no router**.
  `selection: { type:'new' } | { type:'session', id }` is the source of truth. The
  sidebar always shows the list (`SessionList`) + a "New Session" slot. The bottom
  prompt is **`Composer` when `sessionActive`**, else **`DispatchBar`**
  (`sessionActive = tab==='sessions' && selection.type==='session'`,
  `DesktopShell.tsx:372`). All list-nav keys live in `DispatchBar`, so once a session
  is focused there is **no key to return to list nav**. The session subtree
  (`MainPaneSession` + `SessionComposer`) runs under `SessionRuntimeProvider` and has
  **no window keydown listener**.
- **Narrow (<1024px) → routed** (`react-router-dom` v7). `/session/:id` renders a
  full-screen `SessionView` with `onBack={() => navigate('/sessions')}`. `SessionView`
  already has a window keydown listener (`SessionView.tsx:63`) handling `Escape`→back
  (empty-composer + pending-card guards), `Shift+Tab`, and `c`. Gap = no `←`, `Ctrl+Z`.

`DispatchBar` is shared by both layouts, so rebinding its keys fixes both at once.

## Changes

### A. Mobile `SessionView` — add `←` and `Ctrl+Z` (`SessionView.tsx:63` listener)

Compute `inComposerWithText` once, then:

- `e.key === 'ArrowLeft' || e.key === 'Escape'` → if `inComposerWithText` return; if
  `hasOpenAction` return; else `preventDefault()` + `handleBack()`. (`handleBack` =
  `detach(sessionId); onBack()` → `navigate('/sessions')`.) `←` and `Esc` become
  equivalent detach gestures; the empty-prompt gate is the existing composer guard.
- `e.ctrlKey && (e.key === 'z' || 'Z')` → if `inComposerWithText` return (preserve
  textarea undo while editing); else `preventDefault()` + `handleBack()`, **bypassing
  the `hasOpenAction` guard**. The escape hatch.

### B. Desktop `DesktopShell` — add detach-to-list (`DesktopShell.tsx`)

Today there is no in-session keyboard at all. Add a `detachToList()` and a window
keydown listener active only while `sessionActive`:

```
function detachToList() {
  const id = selectedSessionId
  setSelection({ type: 'new' })            // faithful: main pane → WelcomePane
  if (id) {
    const idx = sessions.findIndex((s) => s.id === id)
    if (idx >= 0) setHighlightedIndex(idx) // select the row we just left
  }
  setDispatchFocusNonce((n) => n + 1)      // refocus DispatchBar for ↑/↓ nav
}
```

Listener (registered in a `useEffect` gated on `sessionActive`): read
`inComposerWithText` from `document.activeElement` (composer `aria-label="Message"`);
`Ctrl+Z` (yield to composer-with-text) and `←`/`Esc` (yield to composer-with-text)
both `preventDefault()` + `detachToList()`. Add `const [dispatchFocusNonce,
setDispatchFocusNonce] = useState(0)` and pass `autoFocusKey={dispatchFocusNonce}` to
the `DispatchBar` (currently not passed) so focus returns to it on detach.

**Known divergences (documented, accepted):**
- Detaching shows `WelcomePane` (the "Detach to welcome (faithful)" choice), mirroring
  the TUI's screen swap; `→`/`Enter`/`Alt+n` re-attach.
- The pending-permission-card guard is **not** applied on desktop `←`/`Esc`
  (`hasOpenAction` lives inside the runtime provider subtree, not at `DesktopShell`
  scope). Detach is non-destructive — the session keeps running and the card reappears
  on re-attach — so this is safe. `Ctrl+Z` would bypass that guard anyway.

### C. Rebind rename/close in `DispatchBar` (`DispatchBar.tsx` handleKeyDown) — both layouts

- `r` / `F2` → **`Ctrl+R`** (rename). `preventDefault()` **always** (so the browser
  never reloads and discards an in-progress prompt); fire `onRowAction({type:'rename'})`
  only when the prompt is empty.
- `Delete` → **`Ctrl+X`** (close, two-press guard unchanged). Gate on **empty** prompt
  only — when the prompt has text, let the native "cut" work; when empty,
  `preventDefault()` + `onRowAction({type:'delete'})`.
- Update the press-twice hint copy `DispatchBar.tsx:228` "Press Delete again to close"
  → "Press Ctrl+X again to close", and the prop doc comment at `:17`.

### D. Update the `?` overlay (`ShortcutsHelp.tsx`)

- Actions: `r / F2` → `Ctrl+R` (Rename), `Delete` → `Ctrl+X` (Close).
- In-session: change `Esc` row to `← / Esc` "Detach to session list"; add `Ctrl+Z`
  "Force-detach".

## Intentional deviations (out of scope — need backend or conflict with chat UI)

- `Space` peek panel, `Shift+↑/↓` reorder, `Ctrl+T` pin, `Ctrl+S` grouping.
- `Shift+Enter` stays "newline in draft" (not Agent View's dispatch-and-attach).
- **Mobile origin-row highlight on return** is deferred: returning to `/sessions`
  mounts a fresh `Dashboard` via the route, so selecting the origin row would require
  threading router `state` into `Dashboard`. Low value on a full-screen list; left out
  this round. (Desktop origin-row highlight IS included, per B — it's in-component.)

## Final key map after this change

Dashboard / list context (`DispatchBar`, both layouts):

| Key | Action |
| --- | --- |
| `↑` / `↓` | Move selection (line-edge / empty gated) |
| `→` / `Enter` (empty) | Attach highlighted session |
| `Alt+1–9` | Jump-attach by index |
| `Ctrl+R` | Rename highlighted (was `r`/`F2`) |
| `Ctrl+X` | Close highlighted, two-press (was `Delete`) |
| `Esc` | Clear prompt / deselect |
| `?` | Shortcuts overlay |

Session (attached) context:

| Key | Mobile `SessionView` | Desktop `DesktopShell` |
| --- | --- | --- |
| `←` (empty composer) | Detach → `/sessions` | Detach → WelcomePane, select origin row |
| `Esc` (empty composer) | Detach (existing) | Detach (new) |
| `Ctrl+Z` | Force-detach (unless composer has text) | Force-detach (unless composer has text) |
| `Shift+Tab` | Cycle permission mode (existing) | — (out of scope) |

## Testing (TDD, per constitution Principle V)

Write/extend tests before implementation. Test env: in `web/`,
`node_modules/.bin/vitest run <file>` (node_modules symlinked from the canonical
checkout). Quality gates: `vitest run`, `tsc --noEmit`, `eslint` green.

- `web/src/components/session/__tests__/SessionView.keyboard.test.tsx` (mobile):
  - `←` empty composer → `onBack`; `←` with composer text → no `onBack`; `←` with
    pending card → no `onBack`; `Ctrl+Z` with pending card (composer empty) → `onBack`;
    `Ctrl+Z` with composer text → no `onBack`.
- `web/src/components/__tests__/DesktopShell.test.tsx` (desktop):
  - Focus a session, `←` on empty composer → main pane shows WelcomePane and the row
    is `data-selected`; `Ctrl+Z` likewise; `←` with composer text → stays in session.
- `web/src/components/__tests__/DispatchBar.test.tsx` and `KeyboardSessions.test.tsx`:
  - `Ctrl+R` (empty) → rename; `Ctrl+R` with text → no rename, value preserved.
  - `Ctrl+X` (empty) → delete action / two-press; `Ctrl+X` with text → no delete.
  - Replace existing `r`/`F2`/`{Delete}` expectations; update hint regex to
    `/press ctrl\+x again to close/i`.
- `ShortcutsHelp` copy: assert overlay shows `Ctrl+R` and a `←`/detach entry.

## Files touched

- `web/src/components/session/SessionView.tsx` — mobile `←`, `Ctrl+Z`
- `web/src/components/DesktopShell.tsx` — desktop detach listener + `dispatchFocusNonce`
- `web/src/components/DispatchBar.tsx` — `Ctrl+R`/`Ctrl+X` rebind + hint copy
- `web/src/components/ShortcutsHelp.tsx` — overlay copy
- Test files above
