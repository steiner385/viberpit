import type { StatTile } from '@/ui/StatTiles'

/** Tiles above the automation's tab strip (docs/PAGE-TABS.md §6): six is the
 *  grid's two full rows on a phone, and past six the band stops being a
 *  glance. The server's key order is kept — it is the automation's own
 *  ordering of what matters. */
export const MAX_COUNT_TILES = 6

/** `needs_human` → "Needs human". The server's count keys are snake_case
 *  identifiers; this is the one place they become words. */
export function humanizeKey(key: string): string {
  const words = key.replaceAll('_', ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** `status.counts` as stat tiles: untoned and unclickable, because the counts
 *  do not map to tabs and a tile that navigates nowhere must not look like one
 *  that does. A value that is not a finite number is skipped, not shown. */
export function countTiles(counts: Record<string, number> | null | undefined): StatTile[] {
  if (!counts) return []
  return Object.entries(counts)
    .filter(([, value]) => typeof value === 'number' && Number.isFinite(value))
    .slice(0, MAX_COUNT_TILES)
    .map(([key, value]) => ({ label: humanizeKey(key), value }))
}
