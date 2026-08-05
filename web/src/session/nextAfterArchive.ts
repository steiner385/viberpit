/**
 * Pick the session the detail view should advance to when `removedId` leaves the
 * live list. `orderedIds` is the currently-visible session list in display order
 * (filtered + sorted + collapse-flattened — i.e. exactly what the user sees).
 *
 *   - the row directly below the removed one, else
 *   - the row directly above it (it was the last row), else
 *   - null  → it was the only row, or it isn't in the visible list; the caller
 *             falls back to the sessions list (mobile) / welcome pane (desktop).
 */
export function nextVisibleSessionId(
  orderedIds: readonly string[],
  removedId: string,
): string | null {
  const i = orderedIds.indexOf(removedId)
  if (i === -1) return null
  if (i + 1 < orderedIds.length) return orderedIds[i + 1]
  if (i - 1 >= 0) return orderedIds[i - 1]
  return null
}

/**
 * Advance target for closing (archiving/deleting) an OPEN session. The open
 * session isn't guaranteed to be in the visible list: its group may be collapsed
 * (or collapse while it's open — e.g. status regrouping), or a sidebar search may
 * filter it out. Prefer the visible neighbor; when the visible list can't answer,
 * fall back to `allOrderedIds` — the same ordering with collapse and search
 * ignored — so archiving a hidden-but-open session still advances instead of
 * dead-ending on the empty pane.
 */
export function nextSessionAfterClose(
  visibleIds: readonly string[],
  allOrderedIds: readonly string[],
  removedId: string,
): string | null {
  return (
    nextVisibleSessionId(visibleIds, removedId)
    ?? nextVisibleSessionId(allOrderedIds, removedId)
  )
}
