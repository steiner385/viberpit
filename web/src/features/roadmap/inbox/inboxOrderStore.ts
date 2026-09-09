/**
 * The Inbox column's RENDERED row order, published for the detail pane.
 *
 * Two components need one answer to "what comes after this proposal":
 * `InboxScopeBody` (the list column, in the shell) renders the rows, and
 * `RoadmapPage` (the detail pane) advances to the next one after a decision.
 * They are siblings, not parent and child, so they cannot share the ordering
 * state directly — and each mounting its OWN `useItemOrdering` does not work
 * either: that hook seeds group-by/sort-by from localStorage ONCE and never
 * re-reads, so the moment the user changes the sort or the grouping in the
 * column the pane's copy is stale and "next" stops following what is on
 * screen. (That is exactly the bug this module exists to close.)
 *
 * So the column is the single source of truth for what "next" means: it
 * publishes the ids it actually lists — after grouping, sorting, collapse AND
 * the search box — and the pane reads them. Ids rather than proposals, because
 * the pane already has the queue and only needs the sequence.
 *
 * A module-level store (not context) because the two live in different
 * subtrees of the shell, and because the pane must also cope with the column
 * being absent: on phone the list column is unmounted while a proposal is
 * open, so the store is empty and the pane falls back to the server's order.
 * `clearInboxOrder()` on the column's unmount is what makes "empty" mean
 * "nobody is rendering the list" rather than "the last list was empty".
 */
import { useSyncExternalStore } from 'react'

const EMPTY: readonly string[] = []

let order: readonly string[] = EMPTY
const listeners = new Set<() => void>()

function sameOrder(a: readonly string[], b: readonly string[]): boolean {
  if (a === b) return true
  if (a.length !== b.length) return false
  return a.every((id, i) => id === b[i])
}

/**
 * Publish the order the column is rendering. A shallow-equal list is a no-op:
 * the column re-runs this on every render of a re-derived array, and notifying
 * on an identical sequence would re-render the pane for nothing (and, through
 * `useSyncExternalStore`, hand out a new snapshot every time).
 */
export function setInboxOrder(ids: readonly string[]): void {
  if (sameOrder(order, ids)) return
  // Copied: the caller's array is a render-scoped memo it may keep mutating or
  // discard, and the snapshot has to stay stable between notifications.
  order = [...ids]
  for (const listener of listeners) listener()
}

/** The column is gone (unmounted): nobody is rendering an order any more. */
export function clearInboxOrder(): void {
  setInboxOrder(EMPTY)
}

export function getInboxOrder(): readonly string[] {
  return order
}

export function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => { listeners.delete(onChange) }
}

/** The rendered order, re-rendering the reader whenever the column republishes. */
export function useInboxOrder(): readonly string[] {
  return useSyncExternalStore(subscribe, getInboxOrder, getInboxOrder)
}
