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
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { OverlayEdits, TriageProposal } from '@/api/triage'
import { useAcceptProposal, useRejectProposal, useUpdateOverlay } from '@/api/hooks'
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
  /** Arrived via `e` / `?edit=1`: put the caret in the first score field. */
  autoFocusScores: boolean
  /** The decision is made; the page moves on. */
  onDone: () => void
}

/** The four RICE inputs, in formula order (`server/pm/score.py:RICE_FIELDS`). */
const FIELDS = ['reach', 'impact', 'effort', 'confidence'] as const

/** The list column's key map, which lives on `window` and must not reach past
 *  this pane. `A` is Shift+a, and just as destructive. */
const INBOX_KEYS = new Set(['j', 'k', 'a', 'A', 'r', 'e', 'x'])

const CONFIDENCE_TONE: Record<string, Tone> = { high: 'success', medium: 'info', low: 'neutral' }

const editsOf = (p: TriageProposal): OverlayEdits => ({
  reach: p.reach ?? undefined,
  impact: p.impact ?? undefined,
  effort: p.effort ?? undefined,
  confidence: p.confidence ?? undefined,
  featureId: p.proposedFeatureId,
})

export function ProposalDetail({ proposal: p, capabilities, autoFocusScores, onDone }: ProposalDetailProps) {
  // Scoped to the proposal's OWN product, which is the cache entry the list
  // column's rows came from when the inbox is narrowed — an accept that patched
  // a different key would leave the row sitting in the list it was accepted from.
  const accept = useAcceptProposal(p.projectSlug ?? undefined)
  const reject = useRejectProposal(p.projectSlug ?? undefined)
  const overlay = useUpdateOverlay()

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

  const changed = FIELDS.some((f) => (edits[f] ?? null) !== (p[f] ?? null))
    || (edits.featureId ?? null) !== (p.proposedFeatureId ?? null)
  const score = riceScore(edits.reach, edits.impact, edits.effort, edits.confidence)
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

  const saveAndAccept = async () => {
    // edit-then-accept: persist the overlay edit first, then accept with the
    // same edits in the body. A proposal with no mirrored work item has no
    // overlay row to patch and goes straight to the accept.
    if (p.workItemId) {
      try {
        await overlay.mutateAsync({ itemId: p.workItemId, edit: edits })
      } catch {
        // The hook already toasted. Swallowed rather than rethrown: this runs as
        // a floating promise from an event handler, so an escaping rejection
        // ships a spurious `unhandledrejection` to the server log
        // (`obs/install.ts`). The pane stays open holding the edit — accepting
        // scores the server just refused would land the wrong numbers AND move
        // the user off the only screen that could tell them.
        return
      }
    }
    accept.mutate({ id: p.id, edits })
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
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); void saveAndAccept() }}>
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
          Score: <span className="font-semibold tabular-nums text-ink">{score ?? '—'}</span>
        </p>
        <Field label={CAPABILITY.singular} htmlFor="proposal-capability">
          <Select
            id="proposal-capability"
            aria-label={CAPABILITY.singular}
            value={edits.featureId ?? ''}
            onChange={(e) => setEdits((s) => ({ ...s, featureId: e.target.value || null }))}
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
          <Button type="submit" size="lg" disabled={!changed || accept.isPending || overlay.isPending}>
            Save edits &amp; accept
          </Button>
        </div>
      </form>
    </div>
  )
}
