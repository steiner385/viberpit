//
// Multi-select over a list: the selection set, the shift-range anchor, and the
// prune that drops ids which have left the list.
//
// `SessionList` and `ArchivedSessionList` each carried their own copy of all three.
// They had already drifted in the one way that matters: which list a
// selection is allowed to survive in. This hook keeps BOTH lists explicit
// instead of picking a winner.
import { useState } from 'react'

export interface BulkSelection {
  selected: ReadonlySet<string>
  count: number
  isSelected: (id: string) => boolean
  /** Toggle one row. `shift` extends the range from the anchor over `visibleIds`. */
  toggleOne: (id: string, shift?: boolean) => void
  /** Select every visible id, or clear when they are all selected already. */
  toggleAll: () => void
  clear: () => void
  allSelected: boolean
  someSelected: boolean
}

/**
 * @param visibleIds the grouped/sorted/filtered order the user can see. Shift
 *   ranges and select-all operate over THIS — you can only select what you see.
 * @param presentIds the ids a selection may survive in; defaults to
 *   `visibleIds`. Pass the UNFILTERED list to keep a selection alive while its
 *   group is collapsed or a search hides it (ArchivedSessionList's behaviour);
 *   omit it to prune against the visible set (SessionList's).
 */
export function useBulkSelection(
  visibleIds: readonly string[],
  presentIds: readonly string[] = visibleIds,
): BulkSelection {
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  // Last row toggled WITHOUT shift — the pivot a range extends from.
  const [anchorId, setAnchorId] = useState<string | null>(null)

  // Prune at render time rather than in an effect: React re-renders
  // synchronously when state set during render differs from the current value,
  // and an effect here trips the set-state-in-effect lint rule. Both former
  // copies did it this way.
  if (selected.size > 0) {
    const present = new Set(presentIds)
    let changed = false
    const pruned = new Set<string>()
    for (const id of selected) {
      if (present.has(id)) pruned.add(id)
      else changed = true
    }
    if (changed) setSelected(pruned)
  }

  function toggleOne(id: string, shift = false) {
    const anchorIdx = anchorId === null ? -1 : visibleIds.indexOf(anchorId)
    const end = visibleIds.indexOf(id)
    // `end === -1` means `id` is not in `visibleIds` at all — e.g. a stale
    // shift-click racing a row leaving the visible set. Without this guard
    // `[lo, hi]` becomes `[-1, anchorIdx]` and the range loop reads
    // `visibleIds[-1]` (`undefined`) into the selection on every such click.
    if (shift && anchorIdx !== -1 && end !== -1) {
      const [lo, hi] = anchorIdx < end ? [anchorIdx, end] : [end, anchorIdx]
      setSelected((prev) => {
        const next = new Set(prev)
        for (let i = lo; i <= hi; i++) next.add(visibleIds[i])
        return next
      })
      // The anchor deliberately stays put, so the range can be re-extended in
      // either direction without re-clicking the pivot.
      return
    }
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    setAnchorId(id)
  }

  function toggleAll() {
    setSelected((prev) => {
      const every = visibleIds.length > 0 && visibleIds.every((id) => prev.has(id))
      return every ? new Set() : new Set(visibleIds)
    })
  }

  function clear() {
    setSelected(new Set())
    setAnchorId(null)
  }

  const allSelected = visibleIds.length > 0 && visibleIds.every((id) => selected.has(id))
  const someSelected = visibleIds.some((id) => selected.has(id))

  return {
    selected,
    count: selected.size,
    isSelected: (id) => selected.has(id),
    toggleOne,
    toggleAll,
    clear,
    allSelected,
    someSelected,
  }
}
