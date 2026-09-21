// @vitest-environment node
/**
 * The Inbox scope's pure keyboard state machine (finish-line §4.3, roadmap spec
 * §12) — the reducer, the cursor resolver, the score coercion and the typing
 * guard, with no DOM in sight.
 *
 * Moved here with `inboxKeys.ts` when the Triage page's queue became the
 * Roadmap list column's third scope; the assertions are the ones that guarded
 * the deleted page's keyboard state, unchanged. The clamps and the id-anchored cursor
 * are the whole point: every accept is optimistic and irreversible, so a raw
 * index cursor accepts the row above the one the user is looking at whenever a
 * proposal is prepended between the read and the keystroke.
 */
import { describe, it, expect } from 'vitest'

import {
  inboxReducer, initialInboxState, isTypingTarget, resolveCursorIndex, scoreInputValue,
} from './inboxKeys'

const IDS = ['p1', 'p2', 'p3']

describe('inboxReducer', () => {
  it('j/k move clamps to [0, count-1] and anchors on the row it lands on', () => {
    let s = initialInboxState
    s = inboxReducer(s, { type: 'move', delta: 1, ids: IDS }) // 0 -> 1
    s = inboxReducer(s, { type: 'move', delta: 1, ids: IDS }) // 1 -> 2
    s = inboxReducer(s, { type: 'move', delta: 1, ids: IDS }) // clamp at 2
    expect(s.cursor).toBe(2)
    expect(s.cursorId).toBe('p3')
    s = inboxReducer(s, { type: 'move', delta: -5, ids: IDS }) // clamp at 0
    expect(s.cursor).toBe(0)
    expect(s.cursorId).toBe('p1')
  })

  it('move on an empty queue parks the cursor at 0', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 3, cursorId: 'p4' },
                           { type: 'move', delta: 1, ids: [] })
    expect(s.cursor).toBe(0)
    expect(s.cursorId).toBeNull()
  })

  it('x toggles selection', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    expect(s.selected.has('p1')).toBe(true)
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p1' })
    expect(s.selected.has('p1')).toBe(false)
  })

  it('x anchors the range pivot on the row it ticked', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p2' })
    expect(s.anchorId).toBe('p2')
    // Un-ticking re-anchors too: the pivot is the last row touched, ticked or not.
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p1' })
    expect(s.anchorId).toBe('p1')
  })

  it('selectRange sweeps from the anchor to the clicked row, in either direction', () => {
    const ids = ['p1', 'p2', 'p3', 'p4', 'p5']
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p2' })
    s = inboxReducer(s, { type: 'selectRange', id: 'p4', ids })
    expect([...s.selected].sort()).toEqual(['p2', 'p3', 'p4'])
    // The anchor stays put, so the range re-extends the other way from p2.
    expect(s.anchorId).toBe('p2')
    s = inboxReducer(s, { type: 'selectRange', id: 'p1', ids })
    expect([...s.selected].sort()).toEqual(['p1', 'p2', 'p3', 'p4'])
    // A range that adds nothing keeps the same object.
    expect(inboxReducer(s, { type: 'selectRange', id: 'p3', ids })).toBe(s)
  })

  it('selectRange only sweeps the ids it is given — a decision inside the span is skipped', () => {
    // The component passes the SELECTABLE rows (pending classifications) in
    // rendered order, so a decision row or an auto-accept between two ticked
    // rows is simply not in `ids` and cannot be swept into a bulk accept.
    const selectable = ['p1', 'p3', 'p5']
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    s = inboxReducer(s, { type: 'selectRange', id: 'p5', ids: selectable })
    expect([...s.selected].sort()).toEqual(['p1', 'p3', 'p5'])
  })

  it('selectRange with no usable anchor is a plain toggle', () => {
    const ids = ['p1', 'p2', 'p3']
    // Nothing ticked yet: shift-click behaves like the click it wraps.
    let s = inboxReducer(initialInboxState, { type: 'selectRange', id: 'p2', ids })
    expect([...s.selected]).toEqual(['p2'])
    expect(s.anchorId).toBe('p2')
    // The anchor left the selectable set (accepted away): same fallback, and
    // the clicked row becomes the new pivot.
    s = inboxReducer(s, { type: 'selectRange', id: 'p3', ids: ['p1', 'p3'] })
    expect([...s.selected].sort()).toEqual(['p2', 'p3'])
    expect(s.anchorId).toBe('p3')
    // A stale shift-click on a row that is not selectable any more: toggle, not
    // a `[-1, anchor]` sweep that reads `ids[-1]` into the selection.
    s = inboxReducer(s, { type: 'selectRange', id: 'p9', ids })
    expect(s.selected.has('p9')).toBe(true)
    expect(s.selected.has(undefined as unknown as string)).toBe(false)
  })

  it('selectAll replaces the selection with the given ids', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p9' })
    s = inboxReducer(s, { type: 'selectAll', ids: ['p1', 'p2'] })
    expect([...s.selected].sort()).toEqual(['p1', 'p2'])
    // Already exactly that set: same object, no needless re-render.
    expect(inboxReducer(s, { type: 'selectAll', ids: ['p2', 'p1'] })).toBe(s)
  })

  it('clearSelection drops the anchor with the selection', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    s = inboxReducer(s, { type: 'clearSelection' })
    expect(s.anchorId).toBeNull()
    expect(inboxReducer(s, { type: 'clearSelection' })).toBe(s)
  })

  it('deselect drops one id (a single accept must not leave it for `A`)', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p2' })
    s = inboxReducer(s, { type: 'deselect', id: 'p1' })
    expect([...s.selected]).toEqual(['p2'])
    // A no-op deselect keeps the same object (no needless re-render).
    expect(inboxReducer(s, { type: 'deselect', id: 'p9' })).toBe(s)
  })

  it('pruneSelected drops ids whose row is no longer pending', () => {
    // A row can stop being decidable IN PLACE now that a fresh auto-accept
    // stays visible for its undo window: the id is still on screen, so
    // `rowsChanged` would keep it, and the bulk bar would go on counting it.
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p2' })
    s = inboxReducer(s, { type: 'pruneSelected', pendingIds: ['p2', 'p3'] })
    expect([...s.selected]).toEqual(['p2'])
    // Nothing to prune keeps the same object (no needless re-render).
    expect(inboxReducer(s, { type: 'pruneSelected', pendingIds: ['p2'] })).toBe(s)
    expect(inboxReducer(initialInboxState, { type: 'pruneSelected', pendingIds: [] }))
      .toBe(initialInboxState)
  })

  it('clearSelection empties the set (a bulk accept must not resend dead ids)', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p2' })
    expect(s.selected.size).toBe(2)
    s = inboxReducer(s, { type: 'clearSelection' })
    expect(s.selected.size).toBe(0)
  })

  it('focusRow anchors the cursor where a pointer clicked', () => {
    const s = inboxReducer(initialInboxState, { type: 'focusRow', id: 'p3', index: 2 })
    expect(s.cursorId).toBe('p3')
    expect(s.cursor).toBe(2)
  })

  it('rowsChanged clamps the cursor when the queue shrinks', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 4 },
                           { type: 'rowsChanged', ids: ['p1', 'p2'] })
    expect(s.cursor).toBe(1)
    expect(s.cursorId).toBe('p2')
  })

  it('rowsChanged to an empty queue parks the cursor at 0', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 4, cursorId: 'p5' },
                           { type: 'rowsChanged', ids: [] })
    expect(s.cursor).toBe(0)
    expect(s.cursorId).toBeNull()
  })

  it('rowsChanged follows the anchored row when the queue is PREPENDED to', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 1, cursorId: 'p2' },
                           { type: 'rowsChanged', ids: ['p0', 'p1', 'p2', 'p3'] })
    expect(s.cursorId).toBe('p2')
    expect(s.cursor).toBe(2) // the index followed the row, not the other way round
  })

  it('e enters edit mode and cancel exits', () => {
    let s = inboxReducer(initialInboxState, { type: 'startEdit', id: 'p2' })
    expect(s.editing).toBe('p2')
    s = inboxReducer(s, { type: 'cancelEdit' })
    expect(s.editing).toBeNull()
  })
})

describe('resolveCursorIndex', () => {
  it('follows the anchored id wherever the row moved to', () => {
    const s = { ...initialInboxState, cursor: 0, cursorId: 'p3' }
    expect(resolveCursorIndex(IDS, s)).toBe(2)
  })

  it('falls back to the clamped index once the anchored row is gone', () => {
    const s = { ...initialInboxState, cursor: 1, cursorId: 'p2' }
    // p2 accepted away: the cursor lands on the row that took its place…
    expect(resolveCursorIndex(['p1', 'p3'], s)).toBe(1)
    // …clamped, when it was the last row.
    expect(resolveCursorIndex(['p1'], s)).toBe(0)
  })

  it('is -1 for an empty queue (indexing it yields undefined, which is handled)', () => {
    expect(resolveCursorIndex([], initialInboxState)).toBe(-1)
  })
})

describe('scoreInputValue', () => {
  it('maps a cleared field to undefined, NEVER 0', () => {
    // `Number('') === 0`: effort 0 is below RICE_MIN and 422s the PATCH (which
    // silently skips the chained accept); reach/impact/confidence 0 are in range
    // and persist as a real zero that collapses rice_score.
    expect(scoreInputValue('')).toBeUndefined()
    expect(scoreInputValue('  ')).toBeUndefined()
    expect(scoreInputValue('abc')).toBeUndefined()
    expect(scoreInputValue('0')).toBe(0) // an explicit 0 is still a value
    expect(scoreInputValue('4')).toBe(4)
  })
})

describe('isTypingTarget', () => {
  it('is true for the form controls a shortcut must never steal from', () => {
    for (const tag of ['input', 'textarea', 'select'] as const) {
      expect(isTypingTarget(fakeEl(tag)), tag).toBe(true)
    }
    expect(isTypingTarget(fakeEl('div', { contentEditable: true }))).toBe(true)
    // A contenteditable's descendant counts too — the `closest` leg.
    expect(isTypingTarget(fakeEl('span', { insideEditable: true }))).toBe(true)
  })

  it('is false for ordinary elements, for a window-like target and for null', () => {
    expect(isTypingTarget(fakeEl('div'))).toBe(false)
    // `window` has no `tagName`; so does any other non-element event target.
    expect(isTypingTarget({} as EventTarget)).toBe(false)
    expect(isTypingTarget(null)).toBe(false)
  })
})

/** A DOM-free stand-in: `isTypingTarget` reads `tagName`, `isContentEditable`
 *  and `closest`, and nothing else — this file runs in the node environment. */
function fakeEl(
  tag: string,
  { contentEditable = false, insideEditable = false } = {},
): EventTarget {
  return {
    tagName: tag.toUpperCase(),
    isContentEditable: contentEditable,
    closest: (sel: string) => (insideEditable && sel === '[contenteditable="true"]' ? {} : null),
  } as unknown as EventTarget
}
