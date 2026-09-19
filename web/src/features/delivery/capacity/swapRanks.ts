/**
 * The products table's rank ▲▼ buttons (design 2026-09-19 §7.1) swap the rank
 * of the moved row with its adjacent neighbour: the row takes the neighbour's
 * rank and the neighbour takes the row's. Either side may be unranked (`rank
 * === null`) — a plain swap would then hand the other side a `null`, which
 * fails to move anything relative to the unranked group (nulls sort after
 * every ranked product by `createdAt`, regardless of value). When a side's
 * incoming value would be `null`, it falls back to its own 1-based display
 * POSITION instead, so an adjacent pair of unranked rows still separates into
 * two concrete numbers — the row moving "up" gets the smaller one.
 *
 * Pure and side-effect free: `CapacityDetail` turns its two entries into two
 * sequential `useReorderProject().mutateAsync({ slug, priorityRank })` calls.
 */
export interface RankInput {
  slug: string
  rank: number | null
}

export interface RankPatch {
  slug: string
  priorityRank: number
}

/**
 * `index` is the 0-based position of the row whose button was clicked;
 * `direction` picks the neighbour (`up` → index - 1, `down` → index + 1).
 * Returns `[moverPatch, neighbourPatch]`, or `null` when there is no
 * neighbour in that direction (the row is already first/last).
 */
export function swapRanks(
  products: readonly RankInput[],
  index: number,
  direction: 'up' | 'down',
): [RankPatch, RankPatch] | null {
  const neighbourIndex = direction === 'up' ? index - 1 : index + 1
  if (index < 0 || index >= products.length || neighbourIndex < 0 || neighbourIndex >= products.length) {
    return null
  }
  const row = products[index]!
  const neighbour = products[neighbourIndex]!
  return [
    { slug: row.slug, priorityRank: neighbour.rank ?? neighbourIndex + 1 },
    { slug: neighbour.slug, priorityRank: row.rank ?? index + 1 },
  ]
}
