// One initiative row. Separate from `TreeRowCard` because an initiative is the
// only row that carries a stage and a reason — the two things a human acts on.
//
// `rowRole` exists because this card is rendered in two containers: under the
// Map scope's `role="tree"` (a `treeitem`, with its level) and under the
// Initiatives scope's `GroupedList` panels, which are `role="list"` and whose
// children must be `listitem` (`aria-required-children`).
import { Diamond } from 'lucide-react'
import { CardFrame } from '@/ui/list/CardFrame'
import { Badge } from '@/ui/Badge'
import type { InitiativeNode } from '@/api/roadmap'
import { INITIATIVE, nextStage, reasonLabel, stageLabel } from '@/features/products/vocab'
import { TIER_ACCENT, TIER_TEXT, TIER_TONE, tierLabel } from '../roadmapTier'
import { indentClass } from './indent'
import { TierDot } from './TierDot'
import { FoldChevron } from './FoldChevron'

export interface InitiativeCardProps {
  initiative: InitiativeNode
  rowKey: string
  depth: number
  selected: boolean
  onOpen: () => void
  /** Container-dependent row role; `aria-level` is emitted for a tree row only. */
  rowRole?: 'treeitem' | 'listitem'
  /** Namespace for the open control's DOM id (`ui/list/rowId`). Required in
   *  practice for this card: it is the one row rendered by three containers at
   *  once — the Map tree, the Initiatives scope, and the detail pane's
   *  `InitiativeList` — and `rowKey` is the same slug-derived string in all
   *  three, so an unscoped id collides across panes. */
  idScope?: string
  /** Roving tabindex inside the Map scope's tree (`useTreeKeys`). The
   *  Initiatives scope is a plain list with no cursor of its own, so every row
   *  there stays a tab stop — hence the default. */
  focusable?: boolean
  /** Program fold state — the Map scope's tree passes these three from the row's
   *  `TreeRow` (same shape a capability row already carries); every other
   *  caller (a plain detail list, the Initiatives scope) omits them, which is
   *  the same as `hasChildren: false` — no chevron, no toggle. */
  expanded?: boolean
  hasChildren?: boolean
  onToggle?: () => void
  /** The program a PHASE belongs to, badged before the stage chip. Only the
   *  Initiatives scope (leaf rows grouped by stage) looks this up and passes
   *  it; every other caller omits it. */
  programTitle?: string | null
}

export function InitiativeCard({
  initiative, rowKey, depth, selected, onOpen, rowRole = 'treeitem', idScope, focusable = true,
  expanded = false, hasChildren = false, onToggle, programTitle = null,
}: InitiativeCardProps) {
  const title = initiative.title ?? initiative.slug
  const reason = reasonLabel(initiative.reason)
  // "Ready to advance" alone does not say advance to WHAT. The gate's target is
  // the same stage the initiative page's Advance button names (`nextStage`,
  // in vocab beside `reasonLabel` so the two cannot drift).
  const next = initiative.reason === 'advance' ? nextStage(initiative.stage) : null
  const epic = initiative.epic
  return (
    <div
      data-testid={`row-${rowKey}`}
      data-row-key={rowKey}
      role={rowRole}
      aria-level={rowRole === 'treeitem' ? depth + 1 : undefined}
      // `aria-selected` belongs to a treeitem; on a listitem it is not a valid
      // attribute at all (axe rejects it). Same reasoning for `aria-expanded`
      // below — a program row only carries it inside the Map scope's tree.
      aria-selected={rowRole === 'treeitem' ? selected : undefined}
      aria-expanded={rowRole === 'treeitem' && hasChildren ? expanded : undefined}
      className={indentClass(depth)}
    >
      <CardFrame
        id={rowKey}
        idScope={idScope}
        openLabel={`Open ${INITIATIVE.singular.toLowerCase()} ${title}`}
        railClass={TIER_ACCENT[initiative.tier].rail}
        selected={selected}
        listMode={false}
        focusable={focusable}
        swipeEnabled={false}
        onOpen={onOpen}
        leading={
          hasChildren ? (
            <FoldChevron expanded={expanded} label={title} onToggle={() => onToggle?.()} />
          ) : (
            <span aria-hidden="true" className="size-6 shrink-0" />
          )
        }
        identity={
          <span className="flex min-w-0 items-center gap-2">
            <TierDot tier={initiative.tier} />
            <Diamond size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
            <span className="truncate text-sm font-medium text-ink">{title}</span>
          </span>
        }
        trailing={
          <>
            {programTitle && (
              // `title` + `aria-label` so assistive tech and a hover both read
              // this as "part of the program", not just its bare name — which
              // would otherwise be indistinguishable from the stage badge
              // beside it.
              <Badge tone="neutral" title={`Part of ${programTitle}`} aria-label={`Part of ${programTitle}`}>
                {programTitle}
              </Badge>
            )}
            <Badge tone={TIER_TONE[initiative.tier]}>{stageLabel(initiative.stage)}</Badge>
          </>
        }
      >
        <span className="block truncate text-xs text-ink-muted">
          {reason
            ? <span className={TIER_TEXT[initiative.tier]}>{reason}{next ? ` → ${stageLabel(next)}` : ''}</span>
            : tierLabel(initiative.tier)}
          {epic ? ` · ${epic.done}/${epic.total} done` : ''}
          {initiative.priority != null ? ` · priority ${initiative.priority}` : ''}
        </span>
      </CardFrame>
    </div>
  )
}
