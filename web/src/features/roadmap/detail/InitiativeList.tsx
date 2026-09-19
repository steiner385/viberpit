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

/** `CardFrame` id namespace for this list's rows — the sibling of the tree's
 *  `TREE_ID_SCOPE`. Both containers are on screen at once in the two-column
 *  layout and both key their rows by initiative slug, so without a scope each
 *  would mint the same `row-initiative:<slug>` (#1475). */
const LIST_ID_SCOPE = 'roadmap-initiative-list'

/**
 * Nest PURELY structurally: an item whose `parentInitiative` names another
 * item IN THIS LIST sits under it; everything else — a plain root, or a phase
 * whose program was left out of the list (e.g. `inFlight.ts`'s leaf-only rule
 * dropped it) — is a root. This lets a caller pass a program alongside only
 * the one phase that actually qualifies (the capability In-flight tab: the
 * phase is in flight, its program is shown for context even though it is
 * never counted) without `InitiativeList` knowing anything about tiers itself
 * — that stays `inFlight.ts`'s job.
 *
 * A DFS, not a one-level flatten: a chain deeper than program → phase (e.g. a
 * malformed or future three-level payload, `A → B → C`) must still emit every
 * input exactly once rather than silently dropping `C`. The VISUAL depth is
 * still clamped to 0 | 1 — this list only ever draws two indent levels — but
 * that clamp is cosmetic, not a truncation of the walk. `seen` guards a cyclic
 * `parentInitiative` (a malformed payload) from an infinite recursion.
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
  const seen = new Set<string>()
  const emit = (node: InitiativeNode, depth: number): { node: InitiativeNode; depth: 0 | 1 }[] => {
    if (seen.has(node.slug)) return []
    seen.add(node.slug)
    return [
      { node, depth: (depth === 0 ? 0 : 1) as 0 | 1 },
      ...(byParent.get(node.slug) ?? []).flatMap((c) => emit(c, depth + 1)),
    ]
  }
  return roots.flatMap((r) => emit(r, 0))
}

export interface InitiativeListProps {
  initiatives: InitiativeNode[]
  /**
   * A phase's program title (`lookup.ts:programTitleOf`), for a caller whose
   * list can strand a phase with no structural parent to nest under — e.g. a
   * leaf-only `inFlightInitiatives()` result, which never includes the
   * program itself, so `nest()` below sees the phase as a root and draws it
   * with no context. Omit it and a row simply carries none, same as before.
   */
  programTitleFor?: (initiative: InitiativeNode) => string | null
}

export function InitiativeList({ initiatives, programTitleFor }: InitiativeListProps) {
  const navigate = useNavigate()
  if (initiatives.length === 0) return <EmptyState title={`No ${INITIATIVE.plural.toLowerCase()}`} />
  return (
    <div role="list" aria-label={INITIATIVE.plural} className="flex flex-col gap-1">
      {nest(initiatives).map(({ node, depth }) => (
        <InitiativeCard
          key={node.slug}
          rowKey={initiativeKey(node.slug)}
          idScope={LIST_ID_SCOPE}
          depth={depth}
          initiative={node}
          selected={false}
          rowRole="listitem"
          programTitle={programTitleFor?.(node) ?? null}
          onOpen={() => navigate(initiativePath(node.slug))}
        />
      ))}
    </div>
  )
}
