// The virtualized leaf list is ONE array of rows — section headers and leaves
// alike — so a single virtualizer can window it. Open first, closed below
// (the leaves endpoint already sorts within each), headers carrying the counts.
import type { Leaf } from '@/api/roadmap'

export type LeafListRow =
  | { kind: 'header'; key: string; label: string }
  | { kind: 'leaf'; key: string; leaf: Leaf }

export function leafRows(leaves: Leaf[]): LeafListRow[] {
  const open = leaves.filter((l) => l.state === 'open')
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
