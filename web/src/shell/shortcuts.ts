/** An action performed against the focused session row in list mode. */
export type ListRowAction =
  | { type: 'open' }
  | { type: 'continue' }
  | { type: 'rename' }
  | { type: 'archive' }
  | { type: 'permanent-delete' }
  | { type: 'navigate'; dir: 'up' | 'down' }
  | { type: 'exit' } // back to compose (focus the prompt)
  | { type: 'expand' } // group header: open the group
  | { type: 'collapse' } // group header: close the group
  | { type: 'toggle' } // group header: toggle open/closed

export type KeyboardLike = Pick<
  KeyboardEvent,
  'key' | 'code' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'
>

export type ShortcutGroup = 'compose' | 'list-mode' | 'list-mode-header' | 'in-session'

/**
 * Where a shortcut is allowed to fire. Evaluated against live context on each
 * keydown; the FIRST matching row in `SHORTCUTS` order wins.
 *
 * This is the app's state as a *value*, so the "may this key fire here?" rule
 * lives beside the row that declares the key instead of being re-derived by each
 * listener. The five window/document listeners this registry replaces each
 * answered it their own way — `useGlobalHelpKey` queried
 * `[role="dialog"][aria-modal="true"]`, `useGlobalListNav` queried the looser
 * `[role="dialog"]`, and the two disagreed about whether a docked push-mode
 * `Sheet` blocked a key. One shape, filled once per keydown, is how that stops
 * being possible.
 */
export interface ShortcutContext {
  /** A modal Dialog/Sheet or an open Menu is up. */
  overlayOpen: boolean
  /** A permission or question card is waiting for an answer. */
  hasOpenAction: boolean
  /** A session is open (`/sessions/:id`). */
  sessionOpen: boolean
  /** The Sessions section is current. */
  inSessions: boolean
  /** Kiosk is read-only: nothing that mutates may fire. */
  interactive: boolean
  /** What currently has focus, for text-entry yielding. */
  active: Element | null
}

export interface Shortcut {
  id: string
  /** Display string for the help panel. Use `keysMac` when the platform binding
   *  differs; `displayKeys()` picks between them. */
  keys: string
  keysMac?: string
  description: string
  group: ShortcutGroup
  /** Present ⇒ wired into the SessionCard list-mode handler. */
  action?: ListRowAction
  match?: (e: KeyboardLike) => boolean
  /** Omitted ⇒ always allowed. Read through `shortcutAllowed()`. */
  when?: (c: ShortcutContext) => boolean
}

/** Unmodified key predicate — bails if Ctrl/Alt/Meta is held (Shift allowed). */
export function plain(pred: (e: KeyboardLike) => boolean) {
  return (e: KeyboardLike): boolean => !e.ctrlKey && !e.altKey && !e.metaKey && pred(e)
}

/**
 * Cmd/Win-chord predicate — REQUIRES Meta, bails on Ctrl/Alt (Shift allowed).
 *
 * Deliberately a sibling of `plain()` rather than a loosening of it. `plain()`
 * bails on Meta, which is the only reason `Cmd+A` still selects text in the
 * prompt instead of archiving the focused session, and `Cmd+C` copies instead of
 * continuing it. A row that wants a Cmd chord opts in here; the list-mode rows
 * keep bailing.
 */
export function meta(pred: (e: KeyboardLike) => boolean) {
  return (e: KeyboardLike): boolean => e.metaKey && !e.ctrlKey && !e.altKey && pred(e)
}

/** True on Apple platforms, where the help sheet shows `keysMac`. */
export function isApplePlatform(): boolean {
  if (typeof navigator === 'undefined') return false
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform ?? '')
}

/** The display string for `s` on the platform this browser is running on. */
export function displayKeys(s: Shortcut): string {
  return isApplePlatform() && s.keysMac ? s.keysMac : s.keys
}

/**
 * Whether `s` may fire in `c`. A row with no `when` is always allowed.
 *
 * The default lives here rather than at each call site so a dispatcher cannot
 * spell it `!s.when(c)` and silently make every unconditional row unreachable.
 */
export function shortcutAllowed(s: Shortcut, c: ShortcutContext): boolean {
  return s.when?.(c) ?? true
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
  // ── List mode, group header focused — WIRED ──────────────────────────
  { id: 'header.expand', keys: '→', description: 'Expand the focused group',
    group: 'list-mode-header', action: { type: 'expand' }, match: plain((e) => e.key === 'ArrowRight') },
  { id: 'header.collapse', keys: '←', description: 'Collapse the focused group',
    group: 'list-mode-header', action: { type: 'collapse' }, match: plain((e) => e.key === 'ArrowLeft') },
  { id: 'header.toggle', keys: 'Enter / Space', description: 'Toggle the focused group',
    group: 'list-mode-header', action: { type: 'toggle' },
    match: plain((e) => e.key === 'Enter' || e.key === ' ') },
  { id: 'header.up', keys: '↑', description: 'Move to the previous row',
    group: 'list-mode-header', action: { type: 'navigate', dir: 'up' }, match: plain((e) => e.key === 'ArrowUp') },
  { id: 'header.down', keys: '↓', description: 'Move to the next row',
    group: 'list-mode-header', action: { type: 'navigate', dir: 'down' }, match: plain((e) => e.key === 'ArrowDown') },
  { id: 'header.exit', keys: 'Esc / Tab', description: 'Return to the prompt (compose)',
    group: 'list-mode-header', action: { type: 'exit' },
    match: plain((e) => e.key === 'Escape' || e.key === 'Tab') },
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
  // `Shift+Tab` (cycle permission mode) is deliberately NOT listed: sub-spec B D8
  // removes the binding, and the handler in `session/useInSessionKeys.ts` is gone
  // too — the AppShell cutover made a window-level `preventDefault()` on Shift+Tab
  // untenable (it killed reverse-tab app-wide and escalated a live session's
  // permission mode), so D8's removal was brought forward into this PR. The
  // registry is the only thing the help sheets render from, so dropping the row
  // here is how the documentation goes away — a `HelpSheet`-local filter would be
  // a second, invisible owner of "which shortcuts are documented".
]

export function matchListShortcut(e: KeyboardLike): ListRowAction | undefined {
  return SHORTCUTS.find((s) => s.group !== 'list-mode-header' && s.match?.(e))?.action
}

export function matchHeaderShortcut(e: KeyboardLike): ListRowAction | undefined {
  return SHORTCUTS.find((s) => s.group === 'list-mode-header' && s.match?.(e))?.action
}

export function shortcutsByGroup(group: ShortcutGroup): Shortcut[] {
  return SHORTCUTS.filter((s) => s.group === group)
}
