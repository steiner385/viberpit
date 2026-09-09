// A flat list of initiatives inside a detail panel. It reuses the Map scope's
// row so an initiative looks the same wherever it appears; the container here is
// a plain list, not a tree, so the rows are `listitem`s at depth 0.
import { useNavigate } from 'react-router-dom'
import type { InitiativeNode } from '@/api/roadmap'
import { EmptyState } from '@/ui/EmptyState'
import { INITIATIVE } from '@/features/products/vocab'
import { InitiativeCard } from '../rows/InitiativeCard'
import { initiativeKey } from '../treeRows'
import { initiativePath } from '../roadmapNavState'

export function InitiativeList({ initiatives }: { initiatives: InitiativeNode[] }) {
  const navigate = useNavigate()
  if (initiatives.length === 0) return <EmptyState title={`No ${INITIATIVE.plural.toLowerCase()}`} />
  return (
    <div role="list" aria-label={INITIATIVE.plural} className="flex flex-col gap-1">
      {initiatives.map((i) => (
        <InitiativeCard
          key={i.slug}
          rowKey={initiativeKey(i.slug)}
          depth={0}
          initiative={i}
          selected={false}
          rowRole="listitem"
          onOpen={() => navigate(initiativePath(i.slug))}
        />
      ))}
    </div>
  )
}
