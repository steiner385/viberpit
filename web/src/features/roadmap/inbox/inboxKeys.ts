/**
 * The Triage Inbox's keyboard state machine, extracted as a pure reducer so the
 * cursor/selection rules are testable without a DOM (design §8).
 *
 * The clamps are the point. Every accept/reject is OPTIMISTIC, so the row under
 * the cursor disappears mid-keystroke and the queue can empty entirely while the
 * user is still typing: `move` and `rowsChanged` both have to survive an empty
 * `ids`, and the component has to survive `rows[cursor] === undefined`.
 *
 * The cursor is an ID, not an index. The queue reorders under the user's hands —
 * `useTriageLive` invalidates on every `/ws` status frame, `useTriage` polls
 * every 15 s, and the server returns proposals newest-first so a new one is
 * PREPENDED — and an accept is irreversible (the endpoint 409s anything that is
 * not `pending`, and there is no un-accept route). With a raw index, one
 * proposal arriving between "the user reads row 4" and "the user presses `a`"
 * accepts the row above the one under their eyes, permanently. So `cursorId` is
 * the source of truth and the index is derived from it each render
 * (`resolveCursorIndex`); `cursor` survives only as the fallback for the frame
 * after the anchored row leaves the list, which is what makes an optimistic
 * removal land the cursor on the row that took its place.
 */
export interface InboxState {
  /**
   * Fallback index, used ONLY when `cursorId` no longer names a visible row —
   * i.e. right after the anchored row was accepted/rejected away. Always kept in
   * `[0, count-1]`, or 0 when empty.
   */
  cursor: number
  /** The row the cursor is on, by identity. The source of truth. */
  cursorId: string | null
  /** Proposal ids toggled with `x`, swept together by `A`, `R` or `D`. */
  selected: Set<string>
  /**
   * The last row ticked WITHOUT shift — the pivot a shift-click range extends
   * from (`selectRange`). The same rule `useBulkSelection` keeps for the
   * Sessions lists: it stays put across range extensions, so the range can be
   * re-extended in either direction without re-clicking the pivot.
   */
  anchorId: string | null
  /** Proposal id being edited (`e`), or null. */
  editing: string | null
}

export type InboxEvent =
  // `ids`, not `count`: every cursor move re-anchors on the row it lands on, so
  // the reducer needs the identities and not just how many there are.
  | { type: 'move'; delta: number; ids: string[] }
  | { type: 'focusRow'; id: string; index: number } // pointer: click a row
  | { type: 'toggleSelect'; id: string }
  // Shift-click: select every row between the anchor and `id`, over the
  // SELECTABLE rows in rendered order — the pending classification rows, so a
  // decision or an auto-accepted row inside the span is skipped, not swept.
  | { type: 'selectRange'; id: string; ids: string[] }
  // The header tick: replace the selection with `ids` (the selectable rows).
  | { type: 'selectAll'; ids: string[] }
  | { type: 'deselect'; id: string }
  | { type: 'clearSelection' }
  // The ids still `pending` in the rendered queue; everything else in the
  // selection is dropped.
  | { type: 'pruneSelected'; pendingIds: string[] }
  | { type: 'startEdit'; id: string }
  | { type: 'cancelEdit' }
  | { type: 'rowsChanged'; ids: string[] } // re-anchor/clamp when the queue moves

/**
 * Where the cursor IS, given the queue as it is right now.
 *
 * Returns -1 for an empty queue — the component renders "inbox zero" then, and a
 * caller that indexes with -1 gets `undefined`, which every handler tolerates.
 */
export function resolveCursorIndex(ids: string[], s: InboxState): number {
  if (ids.length === 0) return -1
  if (s.cursorId !== null) {
    const i = ids.indexOf(s.cursorId)
    if (i !== -1) return i
  }
  // The anchored row is gone (accepted, rejected, or filtered away). Its old
  // index now holds the row that took its place, which is where the cursor
  // should land — clamped, because it may have been the last row.
  return Math.min(Math.max(s.cursor, 0), ids.length - 1)
}

/** `cursor`/`cursorId` for an already-resolved index. */
function anchor(s: InboxState, ids: string[], index: number): InboxState {
  const i = ids.length === 0 ? 0 : Math.min(Math.max(index, 0), ids.length - 1)
  const id = ids[i] ?? null
  return i === s.cursor && id === s.cursorId ? s : { ...s, cursor: i, cursorId: id }
}

export function inboxReducer(s: InboxState, e: InboxEvent): InboxState {
  switch (e.type) {
    case 'move': {
      if (e.ids.length === 0) return s.cursorId === null && s.cursor === 0
        ? s
        : { ...s, cursor: 0, cursorId: null }
      return anchor(s, e.ids, resolveCursorIndex(e.ids, s) + e.delta)
    }
    case 'focusRow':
      return s.cursorId === e.id ? s : { ...s, cursor: e.index, cursorId: e.id }
    case 'toggleSelect': {
      const selected = new Set(s.selected)
      if (selected.has(e.id)) selected.delete(e.id)
      else selected.add(e.id)
      return { ...s, selected, anchorId: e.id }
    }
    case 'selectRange': {
      const anchorIdx = s.anchorId === null ? -1 : e.ids.indexOf(s.anchorId)
      const end = e.ids.indexOf(e.id)
      // No pivot to extend from (nothing ticked yet, or the anchor left the
      // selectable set), or a stale click on a row that is no longer selectable:
      // a plain toggle, which is what the click would have meant without shift.
      if (anchorIdx === -1 || end === -1) return inboxReducer(s, { type: 'toggleSelect', id: e.id })
      const [lo, hi] = anchorIdx < end ? [anchorIdx, end] : [end, anchorIdx]
      const selected = new Set(s.selected)
      for (let i = lo; i <= hi; i++) selected.add(e.ids[i])
      return selected.size === s.selected.size ? s : { ...s, selected }
    }
    case 'selectAll': {
      const selected = new Set(e.ids)
      const same = selected.size === s.selected.size && e.ids.every((id) => s.selected.has(id))
      return same ? s : { ...s, selected }
    }
    // A single accept/reject takes its row out of the list, so it must take the
    // row out of the SELECTION too: a later `A` that resends the id gets
    // `not_pending` back and the toast under-reports the sweep.
    case 'deselect': {
      if (!s.selected.has(e.id)) return s
      const selected = new Set(s.selected)
      selected.delete(e.id)
      return { ...s, selected }
    }
    // After a bulk accept the selected ids name rows that no longer exist;
    // keeping them would make the next `A` resend already-accepted proposals.
    case 'clearSelection':
      return s.selected.size === 0 && s.anchorId === null
        ? s
        : { ...s, selected: new Set(), anchorId: null }
    // `deselect` and `clearSelection` both cover a row LEAVING; this covers a
    // row that stays and stops being decidable. A fresh auto-accept remains in
    // the queue for its undo window, so a row ticked while pending can flip to
    // `accepted` in place (the PM Triage loop landing between polls) — and
    // `rowsChanged`, which only knows ids, would happily keep it selected. Its
    // tick would then render checked AND disabled, and the bulk bar would count
    // a row that the send-side filter correctly refuses to send.
    case 'pruneSelected': {
      if (s.selected.size === 0) return s
      const pending = new Set(e.pendingIds)
      const selected = new Set([...s.selected].filter((id) => pending.has(id)))
      return selected.size === s.selected.size ? s : { ...s, selected }
    }
    case 'startEdit':
      return { ...s, editing: e.id }
    case 'cancelEdit':
      return s.editing === null ? s : { ...s, editing: null }
    // Keep the fallback index in step with the anchored row, so that when the
    // anchor DOES vanish the index it falls back to is the anchor's current
    // position rather than wherever it was several reorders ago.
    case 'rowsChanged': {
      if (e.ids.length === 0) {
        return s.cursor === 0 && s.cursorId === null ? s : { ...s, cursor: 0, cursorId: null }
      }
      return anchor(s, e.ids, resolveCursorIndex(e.ids, s))
    }
    default:
      return s
  }
}

export const initialInboxState: InboxState = {
  cursor: 0,
  cursorId: null,
  selected: new Set(),
  anchorId: null,
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
 *
 * NOT the whole guard: a button is not a typing target, and a modal sheet's
 * first focused child is its Close button — see `overlayIsOpen()`
 * (`shell/useOverlayState`), which `InboxScopeBody` checks first.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as (HTMLElement & { isContentEditable?: boolean }) | null
  if (!el || typeof el.tagName !== 'string') return false
  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true
  if (el.isContentEditable) return true
  return typeof el.closest === 'function' && el.closest('[contenteditable="true"]') !== null
}

/**
 * A score input's value as the API wants it: `undefined` for "cleared", never 0.
 *
 * `Number('')` is 0, and 0 is not a neutral value here. `effort: 0` is below
 * `RICE_MIN["effort"] = 1` so the PATCH 422s (`bad_rice_value`) and the accept
 * that was chained behind it silently never runs; `reach`/`impact`/`confidence`
 * of 0 are IN range, so "unset this" persists as a real zero, collapses
 * `rice_score` to 0, and sinks the item to the bottom of the roadmap.
 */
export function scoreInputValue(raw: string): number | undefined {
  if (raw.trim() === '') return undefined
  const n = Number(raw)
  return Number.isFinite(n) ? n : undefined
}
