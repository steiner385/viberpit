import type { StatTile } from '@/ui/StatTiles'
import { TIER_LABEL } from '@/features/products/vocab'

/** Tiles above the automation's tab strip (docs/PAGE-TABS.md §6): four is one
 *  row from `sm` up and two rows on a phone. It was six — three phone rows,
 *  238px of numerals that pushed the tab strip to y=594 of 844 and off the
 *  screen entirely at 200% zoom (2026-10-01 Delivery critique, P1). The
 *  server's key order is kept — it is the automation's own ordering of what
 *  matters. */
export const MAX_COUNT_TILES = 4

/** `needs_human` → "Needs human". The server's count keys are snake_case
 *  identifiers; this is the one place they become words. */
export function humanizeKey(key: string): string {
  const words = key.replaceAll('_', ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** The band's leading tile: the automation's decisions, the same number the
 *  Needs you tab wears, clickable to that tab — tiles lead with the queue
 *  (DESIGN.md), and a tile that maps to a tab must open it. Absent while the
 *  count is still unknown, so the band never shows a 0 that means "not yet". */
export interface NeedsYouTile {
  value: number
  onClick: () => void
}

/** `status.counts` as stat tiles behind the Needs you tile. The server's
 *  counts stay untoned and unclickable: they do not map to tabs, and a tile
 *  that navigates nowhere must not look like one that does. The server tiles
 *  are capped at three whether or not the lead is present, so the band does
 *  not reflow when the actionable fetch lands. A value that is not a finite
 *  number is skipped, not shown. */
export function countTiles(counts: Record<string, number> | null | undefined, needsYou?: NeedsYouTile): StatTile[] {
  if (!counts) return []
  const lead: StatTile[] = needsYou
    ? [{ label: TIER_LABEL['needs-you'], value: needsYou.value, tone: 'warning', onClick: needsYou.onClick }]
    : []
  const rest = Object.entries(counts)
    .filter(([, value]) => typeof value === 'number' && Number.isFinite(value))
    .slice(0, MAX_COUNT_TILES - 1)
    .map(([key, value]) => ({ label: humanizeKey(key), value }))
  return [...lead, ...rest]
}
