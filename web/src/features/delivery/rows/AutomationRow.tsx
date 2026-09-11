// One automation row. Modelled line-for-line on
// `features/roadmap/rows/InitiativeCard.tsx`: a `CardFrame` row carrying the
// tier's rail/dot (reused from `roadmapTier.ts` — never a parallel table, see
// `automationTier.ts`), with the actionable count folded into the tier and
// tier-line text rather than recomputed here.
import type { Automation } from '@/api/automations'
import { CardFrame } from '@/components/card/CardFrame'
import { TIER_ACCENT } from '@/features/roadmap/roadmapTier'
import { TierDot } from '@/features/roadmap/rows/TierDot'
import { Badge } from '@/ui/Badge'
import { automationTier, automationTierLine } from '../automationTier'
import { automationKey } from '../deliveryNavState'

export interface AutomationRowProps {
  automation: Automation
  /** Actionable-item count for this automation, from `useAutomationActionableCounts`. */
  actionable: number
  selected: boolean
  onOpen: (id: string) => void
}

export function AutomationRow({ automation: a, actionable, selected, onOpen }: AutomationRowProps) {
  const tier = automationTier(a, actionable)
  const key = automationKey(a.id)
  return (
    <div
      data-testid={`row-${key}`}
      role="listitem"
      // `aria-selected` belongs to an option/treeitem; on a listitem it is not
      // a valid attribute at all (axe rejects it) — `aria-current` instead.
      aria-current={selected ? 'true' : undefined}
    >
      <CardFrame
        id={a.id}
        openLabel={`Open automation ${a.name}`}
        railClass={TIER_ACCENT[tier].rail}
        selected={selected}
        listMode={false}
        swipeEnabled={false}
        onOpen={onOpen}
        identity={
          <span className="flex min-w-0 items-center gap-2">
            <TierDot tier={tier} />
            <span className="truncate text-sm font-medium text-ink">{a.name}</span>
            {a.activeSessions > 0 && <Badge tone="accent" count={a.activeSessions} />}
          </span>
        }
        trailing={
          <>
            <Badge tone={a.enabled ? 'accent' : 'neutral'}>{a.mode ?? 'off'}</Badge>
            {a.workClass && <Badge tone="neutral">{a.workClass}</Badge>}
          </>
        }
      >
        <span className="block truncate text-xs text-ink-muted">
          {automationTierLine(tier, a, actionable)} · {a.status.text}
        </span>
      </CardFrame>
    </div>
  )
}
