import { describe, it, expect } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useBulkSelection } from '../useBulkSelection'

const IDS = ['a', 'b', 'c', 'd']

describe('useBulkSelection', () => {
  it('toggles one id on and off, and tracks the count', () => {
    const { result } = renderHook(() => useBulkSelection(IDS))
    act(() => result.current.toggleOne('b'))
    expect(result.current.isSelected('b')).toBe(true)
    expect(result.current.count).toBe(1)
    act(() => result.current.toggleOne('b'))
    expect(result.current.isSelected('b')).toBe(false)
    expect(result.current.count).toBe(0)
  })

  it('shift-click selects the inclusive range from the anchor, in visible order', () => {
    const { result } = renderHook(() => useBulkSelection(IDS))
    act(() => result.current.toggleOne('b'))            // anchor
    act(() => result.current.toggleOne('d', true))      // range b..d
    expect([...result.current.selected].sort()).toEqual(['b', 'c', 'd'])
  })

  it('extends a range backwards from the same anchor', () => {
    // The anchor must NOT move on a shift-click, or a range cannot be re-extended
    // in the other direction without re-clicking the pivot.
    const { result } = renderHook(() => useBulkSelection(IDS))
    act(() => result.current.toggleOne('c'))
    act(() => result.current.toggleOne('a', true))
    expect([...result.current.selected].sort()).toEqual(['a', 'b', 'c'])
  })

  it('falls back to a plain toggle when the anchor is no longer visible', () => {
    const { result, rerender } = renderHook(
      ({ visible }) => useBulkSelection(visible, IDS),
      { initialProps: { visible: IDS } },
    )
    act(() => result.current.toggleOne('a'))
    rerender({ visible: ['c', 'd'] })                   // 'a' scrolled out of the visible order
    act(() => result.current.toggleOne('d', true))
    expect([...result.current.selected].sort()).toEqual(['a', 'd'])
  })

  it('toggleAll selects every visible id, then clears when all are already selected', () => {
    const { result } = renderHook(() => useBulkSelection(['a', 'b'], IDS))
    act(() => result.current.toggleAll())
    expect([...result.current.selected].sort()).toEqual(['a', 'b'])
    expect(result.current.allSelected).toBe(true)
    act(() => result.current.toggleAll())
    expect(result.current.count).toBe(0)
  })

  it('reports someSelected without allSelected for a partial selection', () => {
    const { result } = renderHook(() => useBulkSelection(['a', 'b']))
    act(() => result.current.toggleOne('a'))
    expect(result.current.someSelected).toBe(true)
    expect(result.current.allSelected).toBe(false)
  })

  it('drops ids that leave `presentIds` (archived or deleted elsewhere)', () => {
    const { result, rerender } = renderHook(
      ({ present }) => useBulkSelection(present, present),
      { initialProps: { present: IDS } },
    )
    act(() => { result.current.toggleOne('a'); })
    act(() => { result.current.toggleOne('b'); })
    rerender({ present: ['b', 'c'] })
    expect([...result.current.selected]).toEqual(['b'])
  })

  it('keeps a selection that is merely hidden, when presentIds is wider than visibleIds', () => {
    // Collapsing a group must not lose the selection inside it.
    const { result, rerender } = renderHook(
      ({ visible }) => useBulkSelection(visible, IDS),
      { initialProps: { visible: IDS } },
    )
    act(() => result.current.toggleOne('a'))
    rerender({ visible: ['c', 'd'] })
    expect(result.current.isSelected('a')).toBe(true)
  })

  it('a shift-click on an id absent from visibleIds falls back to a plain toggle', () => {
    // 'c' is present (so the render-time prune does not immediately drop it)
    // but NOT visible — unlike the "anchor no longer visible" case above,
    // where the ANCHOR fell out of view, here the just-clicked id is the one
    // missing from `visibleIds`. Without the `end !== -1` guard this produced
    // `[lo, hi] = [-1, anchorIdx]`, whose range loop reads `visibleIds[-1]` —
    // `undefined` — into the selection.
    const { result } = renderHook(() => useBulkSelection(['a', 'b'], IDS))
    act(() => result.current.toggleOne('a'))          // anchor, visible
    act(() => result.current.toggleOne('c', true))    // shift-click, present but not visible
    expect([...result.current.selected].sort()).toEqual(['a', 'c'])
    expect(result.current.selected.has(undefined as unknown as string)).toBe(false)
  })

  it('clear() empties the selection and forgets the anchor', () => {
    const { result } = renderHook(() => useBulkSelection(IDS))
    act(() => result.current.toggleOne('b'))
    act(() => result.current.clear())
    expect(result.current.count).toBe(0)
    // With no anchor, a shift-click is a plain toggle.
    act(() => result.current.toggleOne('d', true))
    expect([...result.current.selected]).toEqual(['d'])
  })
})
