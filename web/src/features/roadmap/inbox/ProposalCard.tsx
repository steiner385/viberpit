/**
 * One pending proposal, as a row of the Roadmap list column's Inbox scope
 * (finish-line §4.3, roadmap spec §12) — a CLASSIFICATION proposal or, since
 * spec 2026-09-18 §6, a DECISION proposal.
 *
 * Built on `CardFrame` like every other row in the column, so a proposal reads
 * the same as a capability or an initiative beside it: rail, open control,
 * identity, a trailing chip, one line of detail. Its `role="listitem"` wrapper
 * is `InitiativeCard`'s arrangement and for the same reason — `GroupedList`
 * gives the Inbox `panelRole="list"` panels, whose children must be listitems
 * (`aria-required-children`), and an `<li>` outside a `<ul>` is not valid HTML.
 *
 * The two kinds are two ROW BODIES inside one wrapper, not two cards: the
 * wrapper carries the cursor ring, the test id and the scroll-into-view that
 * `InboxScopeBody`'s key map depends on, and a second component would be free to
 * spell any of those differently.
 *
 * Confidence is mapped onto the roadmap's OWN tier vocabulary rather than a
 * second colour table: `TierDot` and `Badge` then take their dot and their tone
 * from `roadmapTier.ts`, the column's one source of truth for what a row's
 * urgency looks like.
 */
import { useEffect, useRef } from 'react'
import { Check, Inbox, Scale } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CardFrame } from '@/ui/list/CardFrame'
import { Badge } from '@/ui/Badge'
import { useUndoProposal } from '@/api/hooks'
import type { RoadmapTier } from '@/api/roadmap'
import { isDecisionProposal, type DecisionProposal, type InboxProposal, type TriageProposal } from '@/api/triage'
import { TIER_ACCENT, TIER_TONE } from '../roadmapTier'
import { TierDot } from '../rows/TierDot'
import { isPending } from './triageSelectors'

/** The classifier's confidence, in the roadmap's tier words. An unknown level
 *  (the server may add one) reads as `idle` — calm, never a false alarm. */
const CONF_TIER: Record<string, RoadmapTier> = {
  high: 'in-flight',
  medium: 'proposed',
  low: 'idle',
}

/** A pending decision is the spec's own definition of needs-you (§7: the
 *  roadmap tier is needs-you iff … a pending decision proposal), so the row
 *  wears that tier rather than a colour invented here. */
const DECISION_TIER: RoadmapTier = 'needs-you'

export interface ProposalCardProps {
  proposal: InboxProposal
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
  // A cursor the user cannot SEE must not be the one a keystroke acts on: `a`
  // is one press from an accept that only an undo toast takes back, and after
  // enough `j` the highlighted row is below the fold of the list's scroll band.
  // `CardFrame` scrolls on `selected` — the URL selection — which is a
  // different thing entirely, so the cursor needs its own.
  const rowRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    // jsdom (and any other non-layout host) implements no scrolling at all.
    if (cursor && rowRef.current?.scrollIntoView) rowRef.current.scrollIntoView({ block: 'nearest' })
  }, [cursor])

  return (
    <div
      ref={rowRef}
      data-testid={`row-proposal:${p.id}`}
      role="listitem"
      // The cursor is not the selection: `aria-current` says "the keys act
      // here", `selected` (the ring, below) says "this is open in the pane".
      aria-current={cursor ? 'true' : undefined}
      className={cn(cursor && 'rounded-lg ring-1 ring-accent/40')}
    >
      {isDecisionProposal(p)
        ? <DecisionRow proposal={p} selected={selected} onOpen={onOpen} />
        : (
          <TriageRow
            proposal={p}
            selected={selected}
            checked={checked}
            onOpen={onOpen}
            onToggleSelect={onToggleSelect}
          />
        )}
    </div>
  )
}

function TriageRow({ proposal: p, selected, checked, onOpen, onToggleSelect }: {
  proposal: TriageProposal
  selected: boolean
  checked: boolean
  onOpen: () => void
  onToggleSelect: () => void
}) {
  const name = p.title ?? p.id
  const tier = CONF_TIER[p.confidenceLevel ?? 'low'] ?? 'idle'
  const scores = [p.reach, p.impact, p.effort, p.confidence].map((v) => v ?? '—')
  const pending = isPending(p)
  // The PM Triage loop's own accept, still inside the undo window (spec §8/§9)
  // — the ONE undo path (`useUndoProposal`) is what puts it back to pending.
  const autoAccepted = p.status === 'accepted' && p.classifiedBy === 'auto'
  const undo = useUndoProposal()

  return (
    <CardFrame
      id={`proposal-${p.id}`}
      openLabel={`Open proposal ${name}`}
      // The rail is the row's TIER everywhere else in this column; `checked`
      // is carried by the tick's `aria-pressed` and fill, which is the only
      // affordance a screen reader can hear anyway.
      railClass={TIER_ACCENT[tier].rail}
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
          // A non-pending row (a fresh auto-accept, still in the undo window)
          // has nothing left to select for a bulk accept — disabled rather
          // than hidden, so every row in the list keeps the same leading rail.
          aria-disabled={pending ? undefined : true}
          disabled={!pending}
          onClick={(e) => { e.stopPropagation(); onToggleSelect() }}
          className={cn(
            'flex size-6 shrink-0 items-center justify-center rounded-sm border border-edge',
            checked ? 'bg-accent text-accent-ink' : 'text-ink-faint hover:bg-surface-raised',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            'disabled:opacity-50 disabled:pointer-events-none',
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
          {/* The rail, the dot and the badge tone all say "confidence" in
              colour alone; this is the same fact in words, for a screen
              reader and for anyone who cannot tell the tiers apart. */}
          <span className="sr-only">{p.confidenceLevel ?? 'low'} confidence</span>
          {autoAccepted && <span className="sr-only">accepted automatically</span>}
        </span>
      }
      trailing={
        <span className="flex items-center gap-2">
          {autoAccepted && (
            <>
              <Badge tone="info">auto-accepted</Badge>
              <button
                type="button"
                aria-label={`Undo auto-accept of ${name}`}
                disabled={undo.isPending}
                onClick={(e) => { e.stopPropagation(); undo.mutate(p.id) }}
                className="text-xs text-accent underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                Undo
              </button>
            </>
          )}
          {p.proposedFeatureSlug
            ? <Badge tone={TIER_TONE[tier]}>{p.proposedFeatureSlug}</Badge>
            : <Badge tone="warning">no fit → {p.noFitSuggestion ?? 'new capability'}</Badge>}
        </span>
      }
    >
      <span className="block truncate text-xs text-ink-muted">
        {p.repo}#{p.number} · R{scores[0]} I{scores[1]} E{scores[2]} C{scores[3]}
      </span>
    </CardFrame>
  )
}

/**
 * A decision proposal's row. No tick: `POST /pm/triage/accept-bulk` accepts
 * CLASSIFICATIONS, and accepting a decision needs the option the person chose —
 * which is a thing only the detail pane can ask. So the row's one job is to open
 * it, and the leading slot holds nothing but empty space the width of the
 * classification row's tick: the alignment is kept without drawing a control.
 * A bordered empty box there read, to a sighted user, as a disabled checkbox —
 * an affordance offering something this row can never do.
 */
function DecisionRow({ proposal: p, selected, onOpen }: {
  proposal: DecisionProposal
  selected: boolean
  onOpen: () => void
}) {
  const name = p.title ?? p.ref ?? p.id
  const recommended = p.options.find((o) => o.key === p.recommendedKey)

  return (
    <CardFrame
      id={`proposal-${p.id}`}
      openLabel={`Open decision ${name}`}
      railClass={TIER_ACCENT[DECISION_TIER].rail}
      selected={selected}
      listMode={false}
      focusable
      swipeEnabled={false}
      onOpen={onOpen}
      leading={<span aria-hidden="true" className="size-6 shrink-0" />}
      identity={
        <span className="flex min-w-0 items-center gap-2">
          <TierDot tier={DECISION_TIER} />
          <Scale size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
          <span className="truncate text-sm font-medium text-ink">{name}</span>
          {/* The rail, the dot and the badge say "this is a decision" in colour
              and in a chip that can be truncated away; this says it in words. */}
          <span className="sr-only">awaiting your decision</span>
        </span>
      }
      trailing={
        <Badge tone={TIER_TONE[DECISION_TIER]}>
          {p.options.length} options
        </Badge>
      }
    >
      <span className="block truncate text-xs text-ink-muted">
        {p.ref ?? `${p.repo}#${p.number}`}
        {recommended && <> · recommended: {recommended.label}</>}
      </span>
    </CardFrame>
  )
}
