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
