// The virtualized leaf list is ONE array of rows — section headers and leaves
// alike — so a single virtualizer can window it. Open first, closed below
// with needs-you and blocked issues first; preserve server order otherwise.
import type { Leaf } from '@/api/roadmap'
import { TIER_RANK } from '../roadmapTier'

export type LeafListRow =
  | { kind: 'header'; key: string; label: string }
  | { kind: 'leaf'; key: string; leaf: Leaf }

export function leafRows(leaves: Leaf[]): LeafListRow[] {
  const open = leaves.filter((l) => l.state === 'open').sort((a, b) =>
    Math.min(TIER_RANK[a.tier], TIER_RANK['in-flight']) - Math.min(TIER_RANK[b.tier], TIER_RANK['in-flight']))
  const closed = leaves.filter((l) => l.state !== 'open')
  const rows: LeafListRow[] = []
  if (open.length > 0) {
    rows.push({ kind: 'header', key: 'h:open', label: `Open · ${open.length}` })
    for (const l of open) rows.push({ kind: 'leaf', key: l.id, leaf: l })
  }
  if (closed.length > 0) {
    rows.push({ kind: 'header', key: 'h:closed', label: `Closed · ${closed.length}` })
    for (const l of closed) rows.push({ kind: 'leaf', key: l.id, leaf: l })
  }
  return rows
}
