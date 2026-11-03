/**
 * The session one step from `currentId` in the list's order — Alt+[ / Alt+]
 * and the open session's ‹ › chevrons, which must agree.
 *
 * `orderedIds` is `useSessionListState().ordering.orderedSessions`: what the
 * list renders, filters, search and collapsed groups applied. Wraps at both
 * ends. With no session open — or one the list is not showing — `next` is the
 * first row and `prev` the last, so the key always lands somewhere visible.
 * Null only when there is nowhere else to go: an empty list, or one whose only
 * row is the open session.
 */
export function stepSessionId(
  orderedIds: readonly string[],
  currentId: string | null,
  dir: 'prev' | 'next',
): string | null {
  const n = orderedIds.length
  const i = currentId === null ? -1 : orderedIds.indexOf(currentId)
  if (i === -1) return n === 0 ? null : orderedIds[dir === 'next' ? 0 : n - 1]
  if (n <= 1) return null
  return orderedIds[(i + (dir === 'next' ? 1 : -1) + n) % n]
}
