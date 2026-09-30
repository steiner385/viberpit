/**
 * One pending proposal, as the Inbox scope's DETAIL PANE (roadmap spec §12) —
 * a CLASSIFICATION proposal, or (spec 2026-09-18 §6) a DECISION proposal.
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
 * "Save edits & accept" does NOT PATCH the overlay first. `apply_accept`
 * snapshots the overlay for Undo as it finds it, so a pre-accept PATCH writes
 * the edits into the snapshot and Undo restores the decision it was meant to
 * take back. The accept body carries the same five fields, and the server
 * validates them exactly as the PATCH does — RICE bounds and the existence of
 * `featureId` (`server/api/pm.py:_validate_accept_edits`) — so nothing the
 * PATCH enforced is lost by dropping it.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  isDecisionProposal,
  type DecisionProposal, type InboxProposal, type OverlayEdits, type TriageProposal,
} from '@/api/triage'
import {
  useAcceptDecision, useAcceptProposal, useRejectDecision, useRejectProposal,
} from '@/api/hooks'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { Badge, type Tone } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { CAPABILITY } from '@/features/products/vocab'
import { slugify } from '../slug'
import { ConsequenceList } from '../guidance/ConsequenceList'
import { ScoreFields, SCORE_FIELDS, type ScoreField } from './ScoreFields'
import { riceScore } from './rice'

/** The capability select's sentinel for "stop picking an existing one — I'm
 *  typing a new one instead." Never a real id: capability ids come from the
 *  map, which never mints `__new__`. */
const NEW_CAPABILITY_VALUE = '__new__'

/** A capability the proposal could be homed on — `flattenCapabilities` of its
 *  product, indent and all. */
export interface CapabilityOption {
  id: string
  slug: string
  title: string | null
  depth: number
}

export interface ProposalDetailProps {
  proposal: InboxProposal
  capabilities: readonly CapabilityOption[]
  /**
   * The map query that FILLS `capabilities` has not answered yet.
   *
   * An empty `capabilities` has two very different causes — the product really
   * has no capability to offer, or the map is still in flight — and the select
   * cannot tell them apart on its own. Undifferentiated, it shows one option and
   * a user reasonably concludes the first, then accepts the proposal homed
   * nowhere. Defaulted, so a caller with the options already in hand says nothing.
   */
  capabilitiesLoading?: boolean
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

/**
 * The pane, for whichever kind of proposal is open. Two bodies, one component:
 * the page opens `?proposal=<id>` without knowing which list the id came from,
 * and a second exported pane would need a second branch there and a second copy
 * of the key-swallowing wrapper below.
 */
export function ProposalDetail(props: ProposalDetailProps) {
  return isDecisionProposal(props.proposal)
    ? <DecisionDetail proposal={props.proposal} onDone={props.onDone} />
    : <TriageDetail {...props} proposal={props.proposal} />
}

function TriageDetail({
  proposal: p, capabilities, capabilitiesLoading = false, scopeProduct, autoFocusScores, onDone,
}: ProposalDetailProps & { proposal: TriageProposal }) {
  // Keyed to the SCOPE the list is observing, not to `p.projectSlug`: these
  // mutations remove the row optimistically, and they can only remove it from
  // the cache entry the rows actually came from.
  const accept = useAcceptProposal(scopeProduct ?? undefined)
  const reject = useRejectProposal(scopeProduct ?? undefined)

  const [edits, setEdits] = useState<OverlayEdits>(() => editsOf(p))
  // Inline "+ New capability" (spec 2026-09-22 §3): a triage-created capability
  // lands at root, no parent picker in v1 — reparenting later goes through the
  // Capabilities tab, which already has one.
  const [creatingCapability, setCreatingCapability] = useState(false)
  const [newCapTitle, setNewCapTitle] = useState('')
  const [newCapSlug, setNewCapSlug] = useState('')
  const [slugTouched, setSlugTouched] = useState(false)
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
    // A new proposal is a new decision — the previous one's half-typed
    // capability draft (if any) belongs to the row that just left the queue.
    setCreatingCapability(false)
    setNewCapTitle('')
    setNewCapSlug('')
    setSlugTouched(false)
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
  // BOTH fields, not just the title: `edited.newCapability` below is only
  // emitted when the slug is non-empty too (the title can slugify to '' — a
  // punctuation-only title — and the user can clear the field by hand). If
  // this gate were looser than that one, Save would enable on a state that
  // submits NEITHER `newCapability` NOR `featureId`, silently downgrading to a
  // plain accept onto the AI's original suggestion while the user believes
  // they created a capability.
  const newCapPending = creatingCapability
    && newCapTitle.trim().length > 0 && newCapSlug.trim().length > 0
  const changed = SCORE_FIELDS.some(differs) || featureChanged || newCapPending
  // Only what actually moved: an accept body that restates the proposal's own
  // values is noise in the audit trail and in the diff a reviewer reads.
  const edited: OverlayEdits = {}
  for (const f of SCORE_FIELDS) if (differs(f)) edited[f] = effective[f]
  // `newCapability` and `featureId` are mutually exclusive server-side — typing
  // a new capability's title wins over whatever the select last held.
  if (creatingCapability && newCapTitle.trim() && newCapSlug.trim()) {
    edited.newCapability = { title: newCapTitle.trim(), slug: newCapSlug.trim() }
  } else if (featureChanged) {
    edited.featureId = effective.featureId
  }
  const score = riceScore(effective.reach, effective.impact, effective.effort, effective.confidence)
  const options = useMemo(
    () => capabilities.map((c) => ({ ...c, label: `${'  '.repeat(c.depth)}${c.title ?? c.slug}` })),
    [capabilities],
  )
  const guidance = p.guidance ?? null

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
    <PaneShell>
      <ProposalHeader proposal={p}>
        {p.proposedFeatureSlug
          ? <Badge tone="info">{p.proposedFeatureSlug}</Badge>
          : <Badge tone="warning">no fit → {p.noFitSuggestion ?? 'new capability'}</Badge>}
        <Badge tone={CONFIDENCE_TONE[p.confidenceLevel ?? 'low'] ?? 'neutral'}>
          {p.confidenceLevel ?? 'low'} confidence
        </Badge>
      </ProposalHeader>
      {/* The classifier's stated reason — the one piece of evidence a human has
          before an irreversible accept, so it reads before the form. */}
      {p.rationale && <p className="text-sm text-ink">{p.rationale}</p>}
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); saveAndAccept() }}>
        <ScoreFields edits={edits} onChange={setEdits} firstRef={firstRef} />
        <p className="text-sm text-ink-muted">
          {/* Announced: the score is the consequence of the four numbers above,
              and a sighted user sees it move as they type. */}
          Score: <span aria-live="polite" className="font-semibold tabular-nums text-ink">{score ?? '—'}</span>
        </p>
        <Field label={CAPABILITY.singular} htmlFor="proposal-capability">
          <Select
            id="proposal-capability"
            aria-label={CAPABILITY.singular}
            value={creatingCapability ? NEW_CAPABILITY_VALUE : (edits.featureId ?? '')}
            onChange={(e) => {
              const value = e.target.value
              if (value === NEW_CAPABILITY_VALUE) {
                setCreatingCapability(true)
                setEdits((s) => ({ ...s, featureId: undefined }))
                return
              }
              setCreatingCapability(false)
              setEdits((s) => ({ ...s, featureId: value || undefined }))
            }}
          >
            {/* Empty means "as proposed" — which for a no-fit proposal is no
                capability at all, and the option has to say so rather than
                offering to keep a proposal that does not exist. */}
            <option value="">{p.proposedFeatureId ? '— keep proposal —' : '— no fit —'}</option>
            <option value={NEW_CAPABILITY_VALUE}>+ New {CAPABILITY.singular.toLowerCase()}</option>
            {/* Why the list below is short, while it still is. Disabled so it can
                never be chosen, and inside the select rather than beside it: the
                sentence has to be where the user is looking when they open it. */}
            {capabilitiesLoading && <option value="" disabled>Loading capabilities…</option>}
            {options.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </Select>
        </Field>
        {creatingCapability && (
          // No parent picker in v1: a triage-created capability lands at root;
          // reparenting goes through the Capabilities tab's own dialog.
          <div className="flex flex-col gap-2">
            <Field label={`New ${CAPABILITY.singular.toLowerCase()} title`} htmlFor="new-cap-title">
              <Input
                id="new-cap-title"
                value={newCapTitle}
                onChange={(e) => {
                  const title = e.target.value
                  setNewCapTitle(title)
                  if (!slugTouched) setNewCapSlug(slugify(title))
                }}
              />
            </Field>
            <Field label="Slug" htmlFor="new-cap-slug">
              <Input
                id="new-cap-slug"
                value={newCapSlug}
                onChange={(e) => {
                  setSlugTouched(true)
                  setNewCapSlug(e.target.value)
                }}
              />
            </Field>
          </div>
        )}
        {/* The consequence of each way out, from the owning product's dispatch
            policy (spec 2026-09-17 §4.3). No tick: the Backlog Runner's cadence is
            not the synthesizer's, so the timing says only "next run". */}
        {guidance && (
          <div role="group" aria-label="What each decision does" className="grid gap-3 rounded-md border border-edge p-3 sm:grid-cols-2">
            <ConsequenceList heading="Accept" consequence={guidance.accept} />
            <ConsequenceList heading="Reject" consequence={guidance.reject} />
          </div>
        )}
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
    </PaneShell>
  )
}

/**
 * The pane's frame. The list column's key map lives on `window` and must not
 * reach past this pane (see the header), so the letter keys are swallowed HERE,
 * once, for whichever body is inside — not re-spelled per kind.
 */
function PaneShell({ children }: { children: ReactNode }) {
  return (
    <div
      className="flex flex-col gap-4"
      onKeyDown={(e) => {
        if (e.ctrlKey || e.metaKey || e.altKey) return
        if (INBOX_KEYS.has(e.key)) e.stopPropagation()
      }}
    >
      {children}
    </div>
  )
}

/** Title, issue ref, owning product, when it was proposed — the facts BOTH
 *  kinds carry, said the same way. `children` are the kind's own chips. */
function ProposalHeader({ proposal: p, children }: {
  proposal: InboxProposal
  children?: ReactNode
}) {
  const name = p.title ?? p.id
  return (
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
        {children}
        {isValidTimestamp(p.createdAt) && <span title={p.createdAt}>· proposed {relTime(p.createdAt)}</span>}
      </div>
    </header>
  )
}

/**
 * One pending DECISION proposal (spec 2026-09-18 §6): the options a coding
 * session drafted rather than guessing, as a single-choice group with the
 * recommendation preselected, the rationale beneath, and the same two-column
 * consequence guidance the classification pane carries — through the same
 * `ConsequenceList`, never a second renderer.
 *
 * The recommendation is PRESELECTED, not pre-taken: Accept always sends what the
 * radio group holds, so the person confirms an option either way. There is no
 * undo window on a decision (spec decision 3), which is also why there is no
 * edit-then-accept third button here — the options are the whole of the choice.
 */
function DecisionDetail({ proposal: p, onDone }: {
  proposal: DecisionProposal
  onDone: () => void
}) {
  const accept = useAcceptDecision()
  const reject = useRejectDecision()
  // Seeded from the recommendation, re-seeded only when the PROPOSAL changes —
  // the same render-phase reset the classification pane uses, and for the same
  // reason: the queue refetches on every `/ws` status frame, and an effect on
  // `p` would throw away a choice the user had already clicked.
  const [seededFor, setSeededFor] = useState(p.id)
  const [chosen, setChosen] = useState<string>(() => p.recommendedKey ?? '')
  if (seededFor !== p.id) {
    setSeededFor(p.id)
    setChosen(p.recommendedKey ?? '')
  }
  const groupId = `decision-${p.id}`
  const guidance = p.guidance ?? null

  return (
    <PaneShell>
      <ProposalHeader proposal={p}>
        <Badge tone="warning">decision</Badge>
      </ProposalHeader>
      {/* Why these options and why that recommendation — the evidence the person
          decides on, so it reads before the choice, exactly as the classifier's
          rationale does on the other card. */}
      {p.rationale && <p className="text-sm text-ink">{p.rationale}</p>}
      {p.proposedBy && <p className="text-xs text-ink-muted">Proposed by {p.proposedBy}</p>}
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (!chosen) return
          accept.mutate({ id: p.id, chosenKey: chosen })
          onDone()
        }}
      >
        <div role="radiogroup" aria-label="Options" className="flex flex-col gap-2">
          {p.options.map((o) => (
            <label
              key={o.key}
              htmlFor={`${groupId}-${o.key}`}
              className="flex cursor-pointer items-start gap-2 rounded-md border border-edge p-3 hover:bg-surface-raised"
            >
              <input
                type="radio"
                id={`${groupId}-${o.key}`}
                name={groupId}
                value={o.key}
                checked={chosen === o.key}
                onChange={() => setChosen(o.key)}
                className="mt-1 size-4 shrink-0 accent-accent"
              />
              <span className="flex min-w-0 flex-col gap-1">
                <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
                  {o.label}
                  {o.key === p.recommendedKey && <Badge tone="info">recommended</Badge>}
                </span>
                {o.detail && <span className="text-sm text-ink-muted">{o.detail}</span>}
              </span>
            </label>
          ))}
        </div>
        {guidance && (
          <div role="group" aria-label="What each decision does" className="grid gap-3 rounded-md border border-edge p-3 sm:grid-cols-2">
            <ConsequenceList heading="Accept" consequence={guidance.accept} />
            <ConsequenceList heading="Reject" consequence={guidance.reject} />
          </div>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          {/* Disabled while `chosen` is empty: the server answers 422 on an empty
              `chosen_key`, and a button that can only fail is not an offer.
              DEFENCE ONLY — the state is unreachable today: `decisions.propose`
              refuses a `recommended_key` that is not one of the options, so the
              seed is always a real key, and a native radio group cannot be
              cleared once one is set. It guards the seeding rule rather than any
              flow a person can drive. */}
          {/* One tap by a person (spec decision 7); the radio group stays for a
              different choice. */}
          {p.recommendedKey && (
            <Button
              type="button"
              size="lg"
              disabled={accept.isPending}
              onClick={() => { accept.mutate({ id: p.id, chosenKey: p.recommendedKey! }); onDone() }}
            >
              Accept recommendation
            </Button>
          )}
          <Button type="submit" size="lg" variant={p.recommendedKey ? 'secondary' : undefined} disabled={!chosen || accept.isPending}>
            Accept
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="lg"
            disabled={reject.isPending}
            onClick={() => { reject.mutate({ id: p.id }); onDone() }}
          >
            Reject
          </Button>
        </div>
      </form>
    </PaneShell>
  )
}
