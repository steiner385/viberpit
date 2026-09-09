/**
 * One pending triage proposal, as a row of the Roadmap list column's Inbox
 * scope (finish-line §4.3, roadmap spec §12).
 *
 * Built on `CardFrame` like every other row in the column, so a proposal reads
 * the same as a capability or an initiative beside it: rail, open control,
 * identity, a trailing chip, one line of detail. Its `role="listitem"` wrapper
 * is `InitiativeCard`'s arrangement and for the same reason — `GroupedList`
 * gives the Inbox `panelRole="list"` panels, whose children must be listitems
 * (`aria-required-children`), and an `<li>` outside a `<ul>` is not valid HTML.
 *
 * Confidence is mapped onto the roadmap's OWN tier vocabulary rather than a
 * second colour table: `TierDot` and `Badge` then take their dot and their tone
 * from `roadmapTier.ts`, the column's one source of truth for what a row's
 * urgency looks like.
 */
import { Check, Inbox } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CardFrame } from '@/components/card/CardFrame'
import { Badge } from '@/ui/Badge'
import type { RoadmapTier } from '@/api/roadmap'
import type { TriageProposal } from '@/api/triage'
import { TIER_TONE } from '../roadmapTier'
import { TierDot } from '../rows/TierDot'

/** The classifier's confidence, in the roadmap's tier words. An unknown level
 *  (the server may add one) reads as `idle` — calm, never a false alarm. */
const CONF_TIER: Record<string, RoadmapTier> = {
  high: 'in-flight',
  medium: 'proposed',
  low: 'idle',
}

export interface ProposalCardProps {
  proposal: TriageProposal
  /** Open in the detail pane — the `?proposal=` in the URL. */
  selected: boolean
  /** Ticked for the bulk bar (`x`). */
  checked: boolean
  /** Under the inbox's j/k cursor. */
  cursor: boolean
  onOpen: () => void
  onToggleSelect: () => void
}

export function ProposalCard({
  proposal: p, selected, checked, cursor, onOpen, onToggleSelect,
}: ProposalCardProps) {
  const name = p.title ?? p.id
  const tier = CONF_TIER[p.confidenceLevel ?? 'low'] ?? 'idle'
  const scores = [p.reach, p.impact, p.effort, p.confidence].map((v) => v ?? '—')
  return (
    <div
      data-testid={`row-proposal:${p.id}`}
      role="listitem"
      // The cursor is not the selection: `aria-current` says "the keys act
      // here", `selected` (the ring, below) says "this is open in the pane".
      aria-current={cursor ? 'true' : undefined}
      className={cn(cursor && 'rounded-lg ring-1 ring-accent/40')}
    >
      <CardFrame
        id={`proposal-${p.id}`}
        openLabel={`Open proposal ${name}`}
        railClass={checked ? 'bg-accent' : 'bg-neutral/40'}
        selected={selected}
        listMode={false}
        focusable
        swipeEnabled={false}
        onOpen={onOpen}
        leading={
          <button
            type="button"
            aria-label={`Select ${name}`}
            aria-pressed={checked}
            onClick={(e) => { e.stopPropagation(); onToggleSelect() }}
            className={cn(
              'flex size-6 shrink-0 items-center justify-center rounded-sm border border-edge',
              checked ? 'bg-accent text-accent-ink' : 'text-ink-faint hover:bg-surface-raised',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            )}
          >
            {checked && <Check size={12} aria-hidden="true" />}
          </button>
        }
        identity={
          <span className="flex min-w-0 items-center gap-2">
            <TierDot tier={tier} />
            <Inbox size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
            <span className="truncate text-sm font-medium text-ink">{name}</span>
          </span>
        }
        trailing={
          p.proposedFeatureSlug
            ? <Badge tone={TIER_TONE[tier]}>{p.proposedFeatureSlug}</Badge>
            : <Badge tone="warning">no fit → {p.noFitSuggestion ?? 'new capability'}</Badge>
        }
      >
        <span className="block truncate text-xs text-ink-muted">
          {p.repo}#{p.number} · R{scores[0]} I{scores[1]} E{scores[2]} C{scores[3]}
        </span>
      </CardFrame>
    </div>
  )
}
