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
  // removes the binding, and PR 4 removes the handler in `SessionView.tsx`. The
  // registry is the only thing the help sheets render from, so dropping the row
  // here is how the documentation goes away — a `HelpSheet`-local filter would be
  // a second, invisible owner of "which shortcuts are documented". Between this
  // change and PR 4 the key still works but is undocumented, which is the safer
  // half of the gap to be on.
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
