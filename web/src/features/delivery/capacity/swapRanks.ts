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
 * Fix round 3: that per-pair fallback alone still mis-orders a list where
 * OTHER rows are unranked too — which is every deployment's starting state,
 * since ranking begins with nothing ranked. Handing just the swapped pair two
 * real numbers while the rest stay `null` doesn't keep the swap local: `null`
 * sorts after every ranked value regardless of magnitude, so the pair would
 * jump to the FRONT of the list instead of trading places where they stood.
 * So whenever EITHER side of the swap is unranked, every other visible row is
 * first normalised to its displayed 1-based position (one patch per row whose
 * rank differs from that position) before the pair itself swaps — the whole
 * list becomes a clean, gap-free sequence and the swap lands exactly where it
 * looks like it should. A list where both swap participants are already
 * ranked skips this entirely: no incidental patches for rows that never moved.
 *
 * Pure and side-effect free: `CapacityDetail` applies the returned patches in
 * order, one sequential `useReorderProject().mutateAsync({ slug,
 * priorityRank })` per entry, stopping at the first failure.
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
 * Returns the full ordered list of patches to apply (any normalisation
 * patches first, then the two swap patches), deduped so each slug appears
 * exactly once with its final rank — or `null` when there is no neighbour in
 * that direction (the row is already first/last).
 */
export function swapRanks(
  products: readonly RankInput[],
  index: number,
  direction: 'up' | 'down',
): RankPatch[] | null {
  const neighbourIndex = direction === 'up' ? index - 1 : index + 1
  if (index < 0 || index >= products.length || neighbourIndex < 0 || neighbourIndex >= products.length) {
    return null
  }
  const row = products[index]!
  const neighbour = products[neighbourIndex]!

  const patches: RankPatch[] = []
  if (row.rank === null || neighbour.rank === null) {
    products.forEach((p, i) => {
      if (i === index || i === neighbourIndex) return
      const position = i + 1
      if (p.rank !== position) {
        patches.push({ slug: p.slug, priorityRank: position })
      }
    })
  }
  patches.push({ slug: row.slug, priorityRank: neighbour.rank ?? neighbourIndex + 1 })
  patches.push({ slug: neighbour.slug, priorityRank: row.rank ?? index + 1 })
  return patches
}
