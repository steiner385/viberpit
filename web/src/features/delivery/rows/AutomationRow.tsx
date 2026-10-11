// One automation row. Modelled line-for-line on
// `features/roadmap/rows/InitiativeCard.tsx`: a `CardFrame` row carrying the
// tier's rail/dot (reused from `roadmapTier.ts` — never a parallel table, see
// `automationTier.ts`), with the actionable count folded into the tier and
// tier-line text rather than recomputed here.
import type { Automation } from '@/api/automations'
import { CardFrame } from '@/ui/list/CardFrame'
import { TIER_ACCENT } from '@/features/roadmap/roadmapTier'
import { TierDot } from '@/features/roadmap/rows/TierDot'
import { Badge } from '@/ui/Badge'
import { automationTier, automationTierLine } from '../automationTier'
import { automationKey } from '../deliveryNavState'

export interface AutomationRowProps {
  automation: Automation
  selected: boolean
  /** The list's roving tab stop (`ui/list/useRovingRows`): exactly one row is
   *  `true`. Omitted, `CardFrame` falls back to "focusable when selected",
   *  which leaves an unselected list with no tab stop at all. */
  focusable?: boolean
  /** `CardFrame` id namespace, the one `useRovingRows` looks rows up under. */
  idScope?: string
  onOpen: (id: string) => void
}

export function AutomationRow({ automation: a, selected, focusable, idScope, onOpen }: AutomationRowProps) {
  const tier = automationTier(a)
  const key = automationKey(a.id)
  return (
    <div
      data-testid={`row-${key}`}
      data-row-key={a.id}
      role="listitem"
      // `aria-selected` belongs to an option/treeitem; on a listitem it is not
      // a valid attribute at all (axe rejects it) — `aria-current` instead.
      aria-current={selected ? 'true' : undefined}
    >
      <CardFrame
        id={a.id}
        idScope={idScope}
        openLabel={`Open automation ${a.name}`}
        railClass={TIER_ACCENT[tier].rail}
        selected={selected}
        focusable={focusable}
        listMode={false}
        swipeEnabled={false}
        onOpen={onOpen}
        identity={
          <span className="flex min-w-0 items-center gap-2">
            <TierDot tier={tier} />
            <span className="truncate text-sm font-medium text-ink">{a.name}</span>
            {a.actionableCount != null && a.actionableCount > 0 && <Badge tone="accent" count={a.actionableCount} title="Actionable items" />}
            {/* A paused automation's decisions: a count, never a call for attention (#2936). */}
            {a.parkedCount != null && a.parkedCount > 0 && <Badge tone="neutral" count={a.parkedCount} title="Parked items" />}
          </span>
        }
        trailing={
          // The name is the row's primary identifier, so the trailing badges
          // are kept short: with "Configured mode: propose" spelled out here
          // the name truncated to "S…" in the default-width column (2026-09-27
          // audit). The mode value alone is the pill; the phrase is its title
          // and its accessible text.
          <>
            {(a.operatorTriggered ?? a.modes.length === 0)
              ? <Badge tone="neutral">Invoked tool</Badge>
              : (
                <Badge tone={a.enabled ? 'accent' : 'neutral'} title={`Configured mode: ${a.mode ?? 'unknown'}`}>
                  <span className="sr-only">Configured mode: </span>{a.mode ?? 'unknown'}
                </Badge>
              )}
            {a.workClass && <Badge tone="neutral">{a.workClass}</Badge>}
          </>
        }
      >
        <span className="block truncate text-xs text-ink-muted">
          {automationTierLine(tier, a)} · {a.status.text}
        </span>
      </CardFrame>
    </div>
  )
}
