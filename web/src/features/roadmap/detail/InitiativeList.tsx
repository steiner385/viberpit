// A flat list of initiatives inside a detail panel. It reuses the Map scope's
// row so an initiative looks the same wherever it appears; the container here is
// a plain list, not a tree, so the rows are `listitem`s — never a `treeitem`,
// so no `aria-level` either way.
import { useNavigate } from 'react-router-dom'
import type { InitiativeNode } from '@/api/roadmap'
import { EmptyState } from '@/ui/EmptyState'
import { INITIATIVE } from '@/features/products/vocab'
import { InitiativeCard } from '../rows/InitiativeCard'
import { initiativeKey } from '../treeRows'
import { initiativePath } from '../roadmapNavState'

/**
 * Nest PURELY structurally: an item whose `parentInitiative` names another
 * item IN THIS LIST sits at depth 1 under it; everything else — a plain root,
 * or a phase whose program was left out of the list (e.g. `inFlight.ts`'s
 * leaf-only rule dropped it) — is a root at depth 0. This lets a caller pass a
 * program alongside only the one phase that actually qualifies (the
 * capability In-flight tab: the phase is in flight, its program is shown for
 * context even though it is never counted) without `InitiativeList` knowing
 * anything about tiers itself — that stays `inFlight.ts`'s job.
 */
function nest(initiatives: InitiativeNode[]): { node: InitiativeNode; depth: 0 | 1 }[] {
  const slugs = new Set(initiatives.map((i) => i.slug))
  const byParent = new Map<string, InitiativeNode[]>()
  const roots: InitiativeNode[] = []
  for (const i of initiatives) {
    if (i.parentInitiative && slugs.has(i.parentInitiative)) {
      const list = byParent.get(i.parentInitiative) ?? []
      list.push(i)
      byParent.set(i.parentInitiative, list)
    } else {
      roots.push(i)
    }
  }
  return roots.flatMap((root) => [
    { node: root, depth: 0 as const },
    ...(byParent.get(root.slug) ?? []).map((child) => ({ node: child, depth: 1 as const })),
  ])
}

export function InitiativeList({ initiatives }: { initiatives: InitiativeNode[] }) {
  const navigate = useNavigate()
  if (initiatives.length === 0) return <EmptyState title={`No ${INITIATIVE.plural.toLowerCase()}`} />
  return (
    <div role="list" aria-label={INITIATIVE.plural} className="flex flex-col gap-1">
      {nest(initiatives).map(({ node, depth }) => (
        <InitiativeCard
          key={node.slug}
          rowKey={initiativeKey(node.slug)}
          depth={depth}
          initiative={node}
          selected={false}
          rowRole="listitem"
          onOpen={() => navigate(initiativePath(node.slug))}
        />
      ))}
    </div>
  )
}
