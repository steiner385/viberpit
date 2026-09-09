/**
 * One pending proposal, as the Inbox scope's DETAIL PANE (roadmap spec §12).
 *
 * The editor IS the detail pane: the old standalone page opened an inline form
 * inside the row, which meant the thing you were deciding and the evidence you
 * were deciding it on competed for the same 400px of list. Here the queue stays
 * a queue and this side carries the whole decision — what the classifier
 * proposed, why (the rationale), the four RICE inputs with the score they
 * produce, and the three ways out: accept as proposed, accept with edits, or
 * reject.
 *
 * `onDone` is the page's, not this component's: an accept takes the row out of
 * the queue, so something has to say where the user lands next, and only the
 * page knows the list order.
 *
 * The letter keys are swallowed on the way out (`INBOX_KEYS` below). The list
 * column's key map listens on `window` and is guarded by `isTypingTarget`,
 * which — correctly — reports false for a BUTTON: without this, `a` pressed
 * while Accept has focus would accept whatever row the list's own cursor is on,
 * irreversibly, and it would not be the proposal on screen.
 *
 * "Save edits & accept" does NOT PATCH the overlay first. `_apply_accept`
 * snapshots the overlay for Undo as it finds it, so a pre-accept PATCH writes
 * the edits into the snapshot and Undo restores the decision it was meant to
 * take back. The accept body carries the same five fields, and the server
 * validates them exactly as the PATCH does — RICE bounds and the existence of
 * `featureId` (`server/api/pm.py:_validate_accept_edits`) — so nothing the
 * PATCH enforced is lost by dropping it.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { OverlayEdits, TriageProposal } from '@/api/triage'
import { useAcceptProposal, useRejectProposal } from '@/api/hooks'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { Badge, type Tone } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { CAPABILITY } from '@/features/products/vocab'
import { scoreInputValue } from './inboxKeys'
import { riceScore } from './rice'

/** A capability the proposal could be homed on — `flattenCapabilities` of its
 *  product, indent and all. */
export interface CapabilityOption {
  id: string
  slug: string
  title: string | null
  depth: number
}

export interface ProposalDetailProps {
  proposal: TriageProposal
  capabilities: readonly CapabilityOption[]
  /**
   * The `?product=` narrowing the LIST is observing (`inboxProductForSearch`),
   * not the proposal's own product.
   *
   * It is the accept/reject cache key. In an unscoped inbox the rows come from
   * `[...QK.triage, null]`, so keying the mutation on the proposal's project
   * would have the optimistic removal patch an entry nobody is reading — the
   * decided row would sit there until the next refetch.
   */
  scopeProduct: string | null
  /** Arrived via `e` / `?edit=1`: put the caret in the first score field. */
  autoFocusScores: boolean
  /** The decision is made; the page moves on. */
  onDone: () => void
}

/** The four RICE inputs, in formula order (`server/pm/score.py:RICE_FIELDS`). */
const FIELDS = ['reach', 'impact', 'effort', 'confidence'] as const
type ScoreField = typeof FIELDS[number]

/** The list column's key map, which lives on `window` and must not reach past
 *  this pane. `A` is Shift+a, and just as destructive. */
const INBOX_KEYS = new Set(['j', 'k', 'a', 'A', 'r', 'e', 'x'])

const CONFIDENCE_TONE: Record<string, Tone> = { high: 'success', medium: 'info', low: 'neutral' }

/** What the form starts holding: the proposal's own numbers, so the fields show
 *  what is being decided rather than four blanks. */
const editsOf = (p: TriageProposal): OverlayEdits => ({
  reach: p.reach ?? undefined,
  impact: p.impact ?? undefined,
  effort: p.effort ?? undefined,
  confidence: p.confidence ?? undefined,
  featureId: p.proposedFeatureId ?? undefined,
})

export function ProposalDetail({
  proposal: p, capabilities, scopeProduct, autoFocusScores, onDone,
}: ProposalDetailProps) {
  // Keyed to the SCOPE the list is observing, not to `p.projectSlug`: these
  // mutations remove the row optimistically, and they can only remove it from
  // the cache entry the rows actually came from.
  const accept = useAcceptProposal(scopeProduct ?? undefined)
  const reject = useRejectProposal(scopeProduct ?? undefined)

  const [edits, setEdits] = useState<OverlayEdits>(() => editsOf(p))
  const firstRef = useRef<HTMLInputElement>(null)
  // The pane does not unmount between proposals (the page swaps the prop), so
  // the form is re-seeded when the proposal IDENTITY changes — and only then.
  // Not an effect on `p`: the queue refetches on every `/ws` status frame and
  // on a 15 s poll, each of which hands over a new object for the same
  // proposal, and re-seeding on that would wipe out whatever the user is
  // halfway through typing. Render-phase adjustment (React's documented
  // "resetting state when a prop changes" pattern) rather than a post-paint
  // effect, so the form never shows one frame of the previous proposal's
  // numbers under the new proposal's title.
  const [seededFor, setSeededFor] = useState(p.id)
  if (seededFor !== p.id) {
    setSeededFor(p.id)
    setEdits(editsOf(p))
  }
  useEffect(() => {
    if (autoFocusScores) firstRef.current?.focus()
  }, [autoFocusScores, p.id])

  // A CLEARED field means "keep what the proposal says", not "unset this": the
  // form is a set of overrides over the proposal, and an empty box is the
  // absence of an override. So the score read-out, the changed test and the
  // accept body all read `effective` — before this, clearing `effort` showed
  // "—" while the accept quietly kept the proposal's effort, which is the one
  // combination that lies about what the button is about to do.
  const effective = {
    reach: edits.reach ?? p.reach ?? undefined,
    impact: edits.impact ?? p.impact ?? undefined,
    effort: edits.effort ?? p.effort ?? undefined,
    confidence: edits.confidence ?? p.confidence ?? undefined,
    // The empty option is "— keep proposal —", which is the same absence.
    featureId: edits.featureId === undefined ? (p.proposedFeatureId ?? null) : edits.featureId,
  }
  const differs = (f: ScoreField) => (effective[f] ?? null) !== (p[f] ?? null)
  const featureChanged = (effective.featureId ?? null) !== (p.proposedFeatureId ?? null)
  const changed = FIELDS.some(differs) || featureChanged
  // Only what actually moved: an accept body that restates the proposal's own
  // values is noise in the audit trail and in the diff a reviewer reads.
  const edited: OverlayEdits = {}
  for (const f of FIELDS) if (differs(f)) edited[f] = effective[f]
  if (featureChanged) edited.featureId = effective.featureId
  const score = riceScore(effective.reach, effective.impact, effective.effort, effective.confidence)
  const name = p.title ?? p.id
  const options = useMemo(
    () => capabilities.map((c) => ({ ...c, label: `${'  '.repeat(c.depth)}${c.title ?? c.slug}` })),
    [capabilities],
  )

  const acceptAsProposed = () => {
    accept.mutate({ id: p.id })
    onDone()
  }

  const rejectProposal = () => {
    reject.mutate({ id: p.id })
    onDone()
  }

  // edit-then-accept, in ONE call: `accept` applies the same five fields the
  // overlay PATCH would have, and doing it in one place keeps the undo snapshot
  // (taken inside the accept, from the overlay as it was) honest.
  const saveAndAccept = () => {
    accept.mutate({ id: p.id, edits: edited })
    onDone()
  }

  return (
    <div
      className="flex flex-col gap-4"
      onKeyDown={(e) => {
        if (e.ctrlKey || e.metaKey || e.altKey) return
        if (INBOX_KEYS.has(e.key)) e.stopPropagation()
      }}
    >
      <header className="flex flex-col gap-1">
        {p.url
          ? (
            <a
              href={p.url}
              target="_blank"
              rel="noreferrer"
              className="text-lg font-semibold text-ink hover:underline"
            >
              {name}
            </a>
          )
          : <span className="text-lg font-semibold text-ink">{name}</span>}
        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
          <span>{p.repo}#{p.number}</span>
          {p.projectSlug && <span>· {p.projectSlug}</span>}
          {p.proposedFeatureSlug
            ? <Badge tone="info">{p.proposedFeatureSlug}</Badge>
            : <Badge tone="warning">no fit → {p.noFitSuggestion ?? 'new capability'}</Badge>}
          <Badge tone={CONFIDENCE_TONE[p.confidenceLevel ?? 'low'] ?? 'neutral'}>
            {p.confidenceLevel ?? 'low'} confidence
          </Badge>
          {isValidTimestamp(p.createdAt) && <span title={p.createdAt}>· proposed {relTime(p.createdAt)}</span>}
        </div>
      </header>
      {/* The classifier's stated reason — the one piece of evidence a human has
          before an irreversible accept, so it reads before the form. */}
      {p.rationale && <p className="text-sm text-ink">{p.rationale}</p>}
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); saveAndAccept() }}>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {FIELDS.map((f, i) => (
            <Field key={f} label={f} htmlFor={`score-${f}`}>
              <Input
                id={`score-${f}`}
                type="number"
                aria-label={f}
                ref={i === 0 ? firstRef : undefined}
                value={edits[f] ?? ''}
                onChange={(e) => setEdits((s) => ({ ...s, [f]: scoreInputValue(e.target.value) }))}
              />
            </Field>
          ))}
        </div>
        <p className="text-sm text-ink-muted">
          {/* Announced: the score is the consequence of the four numbers above,
              and a sighted user sees it move as they type. */}
          Score: <span aria-live="polite" className="font-semibold tabular-nums text-ink">{score ?? '—'}</span>
        </p>
        <Field label={CAPABILITY.singular} htmlFor="proposal-capability">
          <Select
            id="proposal-capability"
            aria-label={CAPABILITY.singular}
            value={edits.featureId ?? ''}
            onChange={(e) => setEdits((s) => ({ ...s, featureId: e.target.value || undefined }))}
          >
            {/* Empty means "as proposed" — which for a no-fit proposal is no
                capability at all, and the option has to say so rather than
                offering to keep a proposal that does not exist. */}
            <option value="">{p.proposedFeatureId ? '— keep proposal —' : '— no fit —'}</option>
            {options.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </Select>
        </Field>
        <div className="flex flex-wrap justify-end gap-2">
          {/* `lg` throughout: these are the confirm actions of the pane, and lg
              is the only size carrying the 44px touch target. */}
          <Button type="button" variant="ghost" size="lg" onClick={rejectProposal} disabled={reject.isPending}>
            Reject
          </Button>
          <Button type="button" variant="secondary" size="lg" onClick={acceptAsProposed} disabled={accept.isPending}>
            Accept
          </Button>
          <Button type="submit" size="lg" disabled={!changed || accept.isPending}>
            Save edits &amp; accept
          </Button>
        </div>
      </form>
    </div>
  )
}
