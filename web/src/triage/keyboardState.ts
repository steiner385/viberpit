/**
 * The Triage Inbox's keyboard state machine, extracted as a pure reducer so the
 * cursor/selection rules are testable without a DOM (design §8).
 *
 * The clamps are the point. Every accept/reject is OPTIMISTIC, so the row under
 * the cursor disappears mid-keystroke and the queue can empty entirely while the
 * user is still typing: `move` and `rowsChanged` both have to survive
 * `count === 0`, and the component has to survive `rows[cursor] === undefined`.
 */
export interface InboxState {
  /** Index into the visible rows. Always in `[0, count-1]`, or 0 when empty. */
  cursor: number
  /** Proposal ids toggled with `x`, accepted together by `A`. */
  selected: Set<string>
  /** Proposal id being edited (`e`), or null. */
  editing: string | null
}

export type InboxEvent =
  | { type: 'move'; delta: number; count: number }
  | { type: 'toggleSelect'; id: string }
  | { type: 'clearSelection' }
  | { type: 'startEdit'; id: string }
  | { type: 'cancelEdit' }
  | { type: 'rowsChanged'; count: number } // clamp the cursor when rows shrink

export function inboxReducer(s: InboxState, e: InboxEvent): InboxState {
  switch (e.type) {
    case 'move': {
      if (e.count === 0) return s.cursor === 0 ? s : { ...s, cursor: 0 }
      const next = Math.min(Math.max(s.cursor + e.delta, 0), e.count - 1)
      return next === s.cursor ? s : { ...s, cursor: next }
    }
    case 'toggleSelect': {
      const selected = new Set(s.selected)
      if (selected.has(e.id)) selected.delete(e.id)
      else selected.add(e.id)
      return { ...s, selected }
    }
    // After a bulk accept the selected ids name rows that no longer exist;
    // keeping them would make the next `A` resend already-accepted proposals.
    case 'clearSelection':
      return s.selected.size === 0 ? s : { ...s, selected: new Set() }
    case 'startEdit':
      return { ...s, editing: e.id }
    case 'cancelEdit':
      return s.editing === null ? s : { ...s, editing: null }
    case 'rowsChanged': {
      const cursor = e.count === 0 ? 0 : Math.min(s.cursor, e.count - 1)
      return cursor === s.cursor ? s : { ...s, cursor }
    }
    default:
      return s
  }
}

export const initialInboxState: InboxState = {
  cursor: 0,
  selected: new Set(),
  editing: null,
}

/**
 * Does this keydown belong to something the user is typing into?
 *
 * The inbox listens on `window`, so every keystroke anywhere on the page reaches
 * it — including the ones aimed at a search box, the prompt dock, or the inbox's
 * own inline score editor. Guarding on the TARGET (and not, as the first draft
 * did, only while the editor is open) is what keeps `a`/`r`/`x` from firing
 * mid-word and `A` — which is just Shift+a — from bulk-accepting the queue
 * because someone typed a capital letter.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as (HTMLElement & { isContentEditable?: boolean }) | null
  if (!el || typeof el.tagName !== 'string') return false
  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true
  if (el.isContentEditable) return true
  return typeof el.closest === 'function' && el.closest('[contenteditable="true"]') !== null
}
