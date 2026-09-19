import { IssueDispositionProvider } from './detail/IssueDispositionProvider'
// `/initiatives/:initiativeSlug` — the initiative level, and the only page in
// the Roadmap section with its own prefix (roadmap spec §4). It answers the
// four questions a human has about one initiative: where it is on the track,
// what its epic is doing, which sessions built it, and which capabilities it
// touches — and it carries the three gate controls (Advance, Step back and
// Unblock), each a two-step so a mis-tap never moves the stage.
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  useAcceptMaterialization, useAdvanceInitiative, useInitiative,
  useMaterialization, useRejectMaterialization, useRoadmapMap,
  useStepBackInitiative, useUnblockInitiative,
} from '@/api/hooks'
import { ApiRequestError } from '@/api/client'
import type { ProductNode } from '@/api/roadmap'
import { BRAND_NAME } from '@/brand'
import { createdIssueCount, type Initiative, type InitiativeArtifact, type InitiativeGuidance } from '@/api/sdlc'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Banner } from '@/ui/Banner'
import { Breadcrumb, type BreadcrumbItem } from '@/ui/Breadcrumb'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Tabs } from '@/ui/Tabs'
import { InitiativeTrack } from '@/initiatives/InitiativeTrack'
import { commitUrl, fileUrl, prUrl } from '@/lib/githubUrl'
import { sessionPath } from '@/session/sessionNavState'
import {
  CAPABILITY, EPIC, INITIATIVE, ROADMAP,
  initiativeStatusLabel, nextStage, prevStage, reasonLabel, stageLabel,
} from '@/features/products/vocab'
import { capabilityPath, initiativePath, productPath, roadmapPath } from './roadmapNavState'
import { findProduct, programTitleOf } from './lookup'
import { TIER_TONE } from './roadmapTier'
import { useTabParam } from './useTabParam'
import { StatTiles } from './detail/StatTiles'
import { LeafList } from './detail/LeafList'
import { EditInitiativeDialog } from './detail/EditInitiativeDialog'
import { ConsequenceList } from './guidance/ConsequenceList'
import { WhatHappensNext } from './guidance/WhatHappensNext'

// `epic` and `phases` are mutually exclusive on any one initiative — a
// program's roll-up epic has no pointer of its own to show a per-child list
// for (the leaves endpoint rolls a program up to leaf issues, which would
// disagree with `epic.total`), so a program gets `phases` instead. Both stay
// in the allowed set so `?tab=` validates either; the tab STRIP only ever
// offers the one that matches `initiative.hasChildren` (below).
const TABS = ['track', 'epic', 'phases', 'sessions', 'capabilities'] as const
type Tab = (typeof TABS)[number]

const panelId = (id: string) => `initiative-panel-${id}`

/** One committed artifact, linked at the SHA it was committed at rather than at
 *  HEAD, so the link keeps pointing at the reviewed bytes (`lib/githubUrl`).
 *  Ported verbatim from the Initiative board this page replaced. */
function ArtifactLink({ repo, artifact }: { repo: string | null; artifact: InitiativeArtifact }) {
  const kind = artifact.kind ?? 'artifact'
  if (repo && artifact.repoPath && artifact.commitSha) {
    return (
      <a
        className="text-accent hover:underline"
        target="_blank"
        rel="noreferrer"
        href={fileUrl(repo, artifact.commitSha, artifact.repoPath)}
        title={`${artifact.repoPath} @ ${artifact.commitSha.slice(0, 7)}`}
      >
        {kind} ↗
      </a>
    )
  }
  // A PR URL can arrive without its number (the row stores either), so the
  // number is part of the LABEL only when there is one — "(PR #)" is not a
  // thing a human can read.
  const pr = artifact.prUrl ?? (repo && artifact.prNumber != null ? prUrl(repo, artifact.prNumber) : null)
  if (pr) {
    return (
      <a className="text-accent hover:underline" target="_blank" rel="noreferrer" href={pr}>
        {kind}{artifact.prNumber != null ? ` (PR #${artifact.prNumber})` : ''} ↗
      </a>
    )
  }
  // An artifact row exists as soon as the stage engine starts one; it has no
  // link until it lands somewhere, and saying so beats a dead link.
  return <span className="text-ink-muted" title="not committed yet">{kind} (pending)</span>
}

/**
 * The decomposition gate (spec 003 §7b, #992): what the merged plan would become
 * on GitHub — one epic and its chained task sub-issues — shown while none of it
 * exists yet. It is the last point at which "no" is cheap, so the card shows the
 * whole proposal rather than a count.
 *
 * A DECIDED proposal renders nothing: accept and reject both end the decision,
 * and a card that lingers invites a second tap on a question already answered.
 * A FAILED accept is not a decision — the apply is resumable from its journal
 * and the proposal is still `pending` — so the card stays put with the server's
 * reason on it, and tapping Accept again continues rather than creating a second
 * epic.
 *
 * Accept is a TWO-STEP, the same `Dialog` confirm the page's Advance gate uses
 * and for a stronger reason: it creates issues on GitHub that nothing here can
 * take back, AND it is the plan stage's gate — Materialize is that stage's
 * terminal action, so the server advances the initiative in the same apply and
 * no Advance tap follows. The confirm says so. Reject stays one tap — it decides
 * nothing that cannot be decomposed again on a later tick.
 *
 * Tasks are numbered 1-based FOR THE READER while `dependsOn` stays 0-based on
 * the wire: "Task 1" is what a human counts, and the "after task k" references
 * are mapped through the same +1 so the two always agree on screen.
 */
function MaterializationCard({ slug, stage, guidance }: { slug: string; stage: string | null; guidance: InitiativeGuidance | null }) {
  const { data: proposal } = useMaterialization(slug)
  const accept = useAcceptMaterialization()
  const reject = useRejectMaterialization()
  const [confirming, setConfirming] = useState(false)
  if (!proposal || proposal.status !== 'pending') return null
  // EITHER decision in flight disables BOTH controls: the 409 a double-tap earns
  // is graceful, but a second irreversible write must not be reachable at all.
  const busy = accept.isPending || reject.isPending
  const tasks = proposal.tasks.length
  // A pending proposal with issues behind it is a FAILED apply waiting to resume,
  // and the difference matters before an irreversible tap: Accept continues from
  // those issues, it does not create a second set of them.
  const alreadyCreated = createdIssueCount(proposal.created)
  // The stage the accept itself moves the initiative to: Materialize is the plan
  // stage's terminal action, so the server advances it in the same overlay (spec
  // §7b) rather than waiting for a second Advance tap.
  const advancesTo = nextStage(stage) ?? 'build'
  return (
    <>
      <section
        aria-label="Materialize plan"
        className="flex flex-col gap-3 rounded-md border border-edge bg-surface p-3"
      >
        <div className="flex flex-col gap-1">
          <h2 className="text-sm font-semibold text-ink">Materialize plan</h2>
          <p className="text-sm text-ink">{proposal.epicTitle ?? `Untitled ${EPIC.singular.toLowerCase()}`}</p>
        </div>
        {/* The session's stated reason — the one piece of evidence a human has
            before an accept that writes to GitHub, so it reads before the list. */}
        {proposal.rationale && <p className="text-sm text-ink-muted">{proposal.rationale}</p>}
        {alreadyCreated > 0 && (
          <p className="text-sm text-ink-muted">
            {`${alreadyCreated} issue${alreadyCreated === 1 ? '' : 's'} already created — Accept resumes.`}
          </p>
        )}
        <ul className="flex flex-col gap-1" aria-label="Proposed tasks">
          {proposal.tasks.map((t, i) => (
            // The INDEX is the identity here: `dependsOn` addresses tasks by
            // their position in this array, and no issue number exists to key on
            // yet.
            <li key={i} className="flex items-center gap-2 rounded-md border border-edge p-2 text-sm">
              <span className="shrink-0 tabular-nums text-xs text-ink-muted">Task {i + 1}</span>
              <span className="truncate text-ink">{t.title ?? '(untitled)'}</span>
              {t.dependsOn.length > 0 && (
                <span className="ml-auto shrink-0 text-xs text-ink-muted">
                  after {t.dependsOn.length > 1 ? 'tasks' : 'task'} {t.dependsOn.map((d) => d + 1).join(', ')}
                </span>
              )}
            </li>
          ))}
        </ul>
        {accept.isError && (
          <Banner
            tone="danger"
            title={extractMutationErrorMessage(accept.error, 'Could not materialize this plan')}
          />
        )}
        {reject.isError && (
          <Banner
            tone="danger"
            title={extractMutationErrorMessage(reject.error, 'Could not reject this proposal')}
          />
        )}
        {/* The inbox's decision idiom (`inbox/ProposalDetail`): ghost Reject,
            then the primary, both `lg` — the only size carrying a 44px touch
            target. */}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="ghost" size="lg" onClick={() => reject.mutate(slug)} disabled={busy}>
            Reject
          </Button>
          <Button type="button" size="lg" onClick={() => setConfirming(true)} disabled={busy}>
            Accept
          </Button>
        </div>
        {/* Reject has no confirm (it creates nothing), so its consequence sits
            under the buttons — through the ONE renderer, like every other one. */}
        {guidance && (
          <div className="text-xs">
            <ConsequenceList
              heading="Reject"
              consequence={guidance.actions.materialize_reject}
              tickSeconds={guidance.tickSeconds}
            />
          </div>
        )}
      </section>
      {/* `Dialog` portals to the overlay root, so this sits beside the card in
          the tree without ever being part of the tab panel's scroll. */}
      <Dialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Materialize this ${INITIATIVE.singular.toLowerCase()}'s plan?`}
        size="sm"
        description={guidance
          ? <ConsequenceList consequence={guidance.actions.materialize_accept} tickSeconds={guidance.tickSeconds} />
          : `This creates the ${EPIC.singular.toLowerCase()} and ${tasks} sub-issue${tasks === 1 ? '' : 's'} `
            + `on GitHub. It cannot be undone from ${BRAND_NAME}. `
            + `It also moves this ${INITIATIVE.singular.toLowerCase()} to ${stageLabel(advancesTo)}.`}
      >
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
          {/* `lg` is the only size with a 44px touch target — the house rule for
              a primary confirm in a dialog footer (same as Advance). */}
          <Button
            size="lg"
            aria-label="Confirm materialize"
            loading={accept.isPending}
            onClick={() => accept.mutate(slug, { onSettled: () => setConfirming(false) })}
          >
            Materialize
          </Button>
        </div>
      </Dialog>
    </>
  )
}

function TrackTab({ initiative }: { initiative: Initiative }) {
  const guidance = initiative.guidance ?? null
  // The server sends artifacts in ASCENDING order (intent, spec, plan), so the
  // LAST committed one is the latest — `find` would link the intent commit.
  const newestCommitted = initiative.artifacts.findLast((a) => a.commitSha)
  return (
    <div className="flex flex-col gap-3">
      <MaterializationCard slug={initiative.slug} stage={initiative.stage} guidance={guidance} />
      <InitiativeTrack stage={initiative.stage} status={initiative.status} epic={initiative.epic} />
      {/* What happens with no human action, computed server-side from the same
          engine that acts (spec 2026-09-17 §5). A server without it shows the
          track alone rather than a stale hand-written sentence. */}
      {guidance && <WhatHappensNext guidance={guidance} />}
      <div className="flex flex-wrap items-center gap-3 text-sm">
        {initiative.artifacts.length === 0
          ? <span className="text-ink-muted">No artifacts committed yet.</span>
          : initiative.artifacts.map((a) => <ArtifactLink key={a.id} repo={initiative.repo} artifact={a} />)}
        {initiative.repo && newestCommitted && (
          <a
            className="text-accent hover:underline"
            target="_blank"
            rel="noreferrer"
            href={commitUrl(initiative.repo, newestCommitted.commitSha!)}
          >
            latest commit ↗
          </a>
        )}
      </div>
    </div>
  )
}

// The sub-issues come from the leaves endpoint (tier, blockers, live session,
// and the Remove disposition), not from the detail payload's `children`, which
// stays for the assistant.
function EpicTab({ initiative, product }: { initiative: Initiative; product: ProductNode | null }) {
  const epic = initiative.epic
  if (!epic) {
    return (
      <EmptyState
        title={`No ${EPIC.singular.toLowerCase()} yet`}
        description="One appears when the plan is materialized into GitHub issues."
      />
    )
  }
  return (
    <div className="flex flex-col gap-3">
      {/* A program's roll-up epic can report real sub-issue counts with no
          pointer of its own (`ref`/`url: null`) — Task 13's job to give that
          case its own presentation; this minimal guard just keeps the type
          change from breaking the page. */}
      {epic.url && (
        <a href={epic.url} target="_blank" rel="noreferrer" className="text-sm text-accent hover:underline">
          {epic.ref ?? 'Epic'} ↗
        </a>
      )}
      <StatTiles
        tiles={[
          { label: 'Done', value: epic.done, tone: 'success' },
          { label: 'Total', value: epic.total },
          { label: 'Blocked', value: epic.blocked, tone: 'danger' },
          { label: 'Held', value: epic.held, tone: 'warning' },
        ]}
      />
      <LeafList scope={{ initiative: initiative.slug }} product={product} />
    </div>
  )
}

// A program's phases (replaces the Epic tab — see the `TABS` comment above).
// The server's `GET /api/pm/leaves?initiative=<program>` rolls a program up to
// its leaf descendants' issues precisely so a program page can show them —
// same tiles, same list `EpicTab` renders from its own `epic`, just scoped to
// the program's slug instead of one leaf's.
function PhasesTab({ initiative, product }: { initiative: Initiative; product: ProductNode | null }) {
  const epic = initiative.epic
  return (
    <div className="flex flex-col gap-3">
      {initiative.children.length === 0
        ? <EmptyState title="No phases yet" />
        : (
          <ul className="flex flex-col gap-1" aria-label="Phases">
            {initiative.children.map((c) => (
              <li key={c.slug} className="flex items-center gap-2 rounded-md border border-edge bg-surface p-2 text-sm">
                <Link to={initiativePath(c.slug)} className="text-ink hover:underline">
                  {c.title ?? c.slug}
                </Link>
                <Badge tone="neutral">{stageLabel(c.stage)}</Badge>
              </li>
            ))}
          </ul>
        )}
      {epic && (
        <StatTiles
          tiles={[
            { label: 'Done', value: epic.done, tone: 'success' },
            { label: 'Total', value: epic.total },
            { label: 'Blocked', value: epic.blocked, tone: 'danger' },
            { label: 'Held', value: epic.held, tone: 'warning' },
          ]}
        />
      )}
      <LeafList scope={{ initiative: initiative.slug }} product={product} />
    </div>
  )
}

function SessionsTab({ initiative }: { initiative: Initiative }) {
  const rows = initiative.buildSessions ?? []
  if (rows.length === 0) return <EmptyState title="No sessions yet" description="Sessions appear here as the build stage runs them." />
  return (
    <ul className="flex flex-col gap-1" aria-label="Sessions">
      {rows.map((s) => (
        <li key={s.id} className="rounded-md border border-edge bg-surface p-2 text-sm">
          <Link to={sessionPath(s.id)} className="text-ink hover:underline">{s.branch ?? s.id.slice(0, 8)}</Link>
          <span className="ml-2 text-xs text-ink-muted">{s.status ?? '—'}</span>
        </li>
      ))}
    </ul>
  )
}

function CapabilitiesTab({ initiative, onEdit }: {
  initiative: Initiative
  /** `null` until the map the dialog picks from has arrived. */
  onEdit: (() => void) | null
}) {
  const { projectSlug } = initiative
  // "Edit capabilities" surfaces from this tab too, not just the header — the
  // empty state is exactly where a human wants to fix a missing home. Same gate
  // as the header's Edit: no map, no dialog to open.
  const editButton = (
    <Button
      size="sm"
      variant="ghost"
      disabled={!onEdit}
      title={onEdit ? undefined : 'Loading the map…'}
      onClick={() => onEdit?.()}
    >
      Edit {CAPABILITY.plural.toLowerCase()}
    </Button>
  )
  if (!projectSlug || initiative.capabilities.length === 0) {
    return (
      <div className="flex flex-col gap-3">
        <EmptyState
          title={`No ${CAPABILITY.plural.toLowerCase()} linked`}
          description={`Triage gives an ${INITIATIVE.singular.toLowerCase()} its home on the map.`}
        />
        {editButton}
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      {editButton}
      <ul className="flex flex-col gap-1" aria-label={CAPABILITY.plural}>
        {initiative.capabilities.map((c) => (
          <li key={c.id} className="rounded-md border border-edge bg-surface p-2 text-sm">
            <Link to={capabilityPath(projectSlug, c.slug)} className="text-ink hover:underline">
              {c.title ?? c.slug}
            </Link>
            {c.isPrimary && <Badge tone="accent" className="ml-2">primary</Badge>}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function InitiativePage() {
  return <IssueDispositionProvider><InitiativePageContent /></IssueDispositionProvider>
}

function InitiativePageContent() {
  const { initiativeSlug = '' } = useParams()
  const { data: initiative, isLoading, isError, error } = useInitiative(initiativeSlug || null)
  // The map is the only place the tier/reason is computed (server `pm/tree.py`),
  // and the list column beside this page is already paying for the query — so
  // the header reads WHY this row needs a human from there rather than
  // re-deriving it from the detail payload.
  // `poll: false`: the list column beside this page observes the same cache
  // entry and owns the interval — two polling observers are two requests.
  const { data: map } = useRoadmapMap({ poll: false })
  const advance = useAdvanceInitiative()
  const stepBack = useStepBackInitiative()
  const unblock = useUnblockInitiative()
  const { data: proposal } = useMaterialization(initiativeSlug || null)
  const [tab, setTab] = useTabParam(TABS, 'track')
  const [confirming, setConfirming] = useState(false)
  const [confirmingBack, setConfirmingBack] = useState(false)
  const [confirmingUnblock, setConfirmingUnblock] = useState(false)
  const [editing, setEditing] = useState(false)

  // The slug is the fallback title, so a deep link names something rather than
  // rendering a blank heading while the detail is in flight.
  const title = initiative?.title ?? initiativeSlug
  const node = map?.products.flatMap((p) => p.initiatives).find((i) => i.slug === initiativeSlug)
  const reason = reasonLabel(node?.reason)
  const next = nextStage(initiative?.stage)
  const prev = prevStage(initiative?.stage)
  const blocked = initiative?.status === 'blocked'
  // NOT `inbox/triageSelectors`' `isPending`: this `proposal` is a
  // `MaterializationProposal` (initiative → work items), a different domain that
  // merely shares the word. Routing it through the triage selector would tie two
  // unrelated vocabularies together.
  const proposalPending = proposal?.status === 'pending'

  const guidance = initiative?.guidance ?? null
  // The server's availability reason wins over the client's guess: the two agree
  // by construction (explain.py mirrors the 409 rules), but only one is authoritative.
  const tip = (control: keyof InitiativeGuidance['actions'], fallback: string | undefined) =>
    guidance && !guidance.actions[control].available
      ? (guidance.actions[control].unavailableReason ?? fallback)
      : fallback

  // A renamed slug or a stale bookmark is an ADDRESS problem, not a connection
  // problem, and the two want opposite copy. The detail endpoint answers 404
  // (which `shouldRetryQuery` fails fast, so it lands as isError with no data —
  // there is no "resolved empty" state for react-query to produce), and the map
  // having loaded without this slug says the same thing from the other side.
  const notFound = !initiative
    && ((error instanceof ApiRequestError && error.status === 404)
      || (!isLoading && map !== undefined && node === undefined))

  // The map is where a product's display NAME lives; the detail payload only
  // carries its slug, and a slug among titles reads as a different kind of thing.
  const productSlug = initiative?.projectSlug ?? node?.projectSlug ?? null
  const productNode = productSlug ? findProduct(map, productSlug) : null
  const productName = productSlug ? (productNode?.name ?? productSlug) : null

  // A phase names the program it belongs to (`parentInitiativeSlug`); the map
  // is the only place a display TITLE lives for that slug — same reasoning as
  // `productName` above, read from the same query rather than a second fetch.
  // `lookup.ts:programTitleOf`'s map-wide form: the detail payload carries only
  // the parent's slug, not which product's tree it lives in.
  const programSlug = initiative?.parentInitiativeSlug ?? null
  const programTitleFor = useMemo(() => (map ? programTitleOf(map) : null), [map])
  const programTitle = programTitleFor ? programTitleFor(programSlug) : programSlug

  const crumbs: BreadcrumbItem[] = [
    { label: ROADMAP.label, to: roadmapPath('initiatives') },
    ...(productSlug && productName ? [{ label: productName, to: productPath(productSlug) }] : []),
    { label: title },
  ]

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader
        title={title}
        meta={
          <div className="flex flex-col gap-1">
            <Breadcrumb items={crumbs} />
            {initiative && (
              <div className="flex flex-wrap items-center gap-2">
                {/* Same badge the row in the list column beside this page shows
                    (`rows/InitiativeCard`): the stage, toned by the tier. */}
                <Badge tone={node ? TIER_TONE[node.tier] : 'neutral'}>{stageLabel(initiative.stage)}</Badge>
                <Badge tone={initiative.status === 'blocked' ? 'danger' : 'neutral'}>
                  {initiativeStatusLabel(initiative.status)}
                </Badge>
                {reason && <Badge tone="warning">{reason}</Badge>}
                {/* What the synthesizer did with this row's gate under the product's
                    risk tolerance (spec 2026-09-14 D5): "auto · advanced to plan ·
                    Step back". The reverse it names is the control beside it, never
                    a new undo path; the chip leaves after 24 h (server-side window). */}
                {node?.autoResolved && (
                  <Badge
                    tone={node.autoResolved.outcome === 'refused' ? 'warning' : 'neutral'}
                    title={`Resolved automatically under ${node.autoResolved.tolerance ?? 'the product policy'}`}
                  >
                    {`auto · ${node.autoResolved.summary}${node.autoResolved.reverse ? ` · ${node.autoResolved.reverse}` : ''}`}
                  </Badge>
                )}
                {initiative.repo && <span className="text-xs text-ink-muted">{initiative.repo}</span>}
              </div>
            )}
            {/* A phase names the program it belongs to — same "Part of" idiom a
                sub-capability's own page uses for its parent capability
                (`CapabilityDetail`'s Overview panel). */}
            {initiative && programSlug && (
              <p className="text-xs text-ink-muted">
                Part of{' '}
                <Link className="text-accent hover:underline" to={initiativePath(programSlug)}>
                  {programTitle}
                </Link>
              </p>
            )}
          </div>
        }
        actions={initiative && (
          <div className="flex items-center gap-2">
            {/* The one place an initiative's own facts are edited; the gate
                controls beside it move it, this names it. The dialog needs the
                map (it picks a capability out of it), so the button waits for
                it rather than opening onto nothing. */}
            <Button
              size="sm"
              variant="ghost"
              disabled={!map}
              title={map ? undefined : 'Loading the map…'}
              onClick={() => setEditing(true)}
            >
              Edit
            </Button>
            {/* The undo for a mis-tapped Advance. Disabled, not absent, at intake
                for the same reason Advance is at done: the row of controls must
                not change shape as the initiative moves. */}
            <Button
              size="sm"
              variant="ghost"
              disabled={!prev || blocked || stepBack.isPending}
              onClick={() => setConfirmingBack(true)}
              title={tip('step_back', blocked ? 'Clear the block first' : prev ? `Move the gate back to ${stageLabel(prev)}` : 'Already at the first stage')}
            >
              Step back
            </Button>
            {/* Only a blocked row can be unblocked; a button that is usually
                disabled would read as a control that never works. */}
            {blocked && (
              <Button
                size="sm"
                variant="danger"
                disabled={proposalPending || unblock.isPending}
                onClick={() => setConfirmingUnblock(true)}
                title={tip('unblock', proposalPending ? 'Accept or reject the pending materialization first' : 'Clear the block without moving the gate')}
              >
                Unblock
              </Button>
            )}
            {/* Disabled rather than absent at the end of the lifecycle: the gate is
                the page's one action, and a header that loses its only button reads
                as a page that failed to load. A PROGRAM is the one exception — it
                has no gate of its own (its phases each carry theirs), and the
                server 409s an advance on it, so the control is absent rather than
                a disabled control that always fails. */}
            {!initiative.hasChildren && (
              <Button
                size="sm"
                disabled={!next || blocked || advance.isPending}
                onClick={() => setConfirming(true)}
                title={tip('advance', blocked ? 'Clear the block first' : next ? `Move the gate to ${stageLabel(next)}` : 'Already at the final stage')}
              >
                {next ? `Advance → ${stageLabel(next)}` : 'Complete'}
              </Button>
            )}
          </div>
        )}
      />
      <PageContent busy={isLoading && !initiative}>
        {/* Errors are a Banner, first child of the content region (same idiom,
            and same position, as RoadmapPage) — but a 404 is not a failure to
            report, it is an address that no longer exists, and it gets the
            not-found empty state below instead. */}
        {isError && !notFound && <Banner tone="danger" className="mb-4" title="Could not load this initiative." />}
        {advance.isError && (
          <Banner
            tone="danger"
            className="mb-4"
            title={extractMutationErrorMessage(advance.error, 'Could not advance initiative')}
          />
        )}
        {stepBack.isError && (
          <Banner tone="danger" className="mb-4" title={extractMutationErrorMessage(stepBack.error, 'Could not step back')} />
        )}
        {unblock.isError && (
          <Banner tone="danger" className="mb-4" title={extractMutationErrorMessage(unblock.error, 'Could not clear the block')} />
        )}
        {initiative && (
          <div className="flex flex-col gap-4">
            <Tabs
              label={title}
              value={tab}
              onChange={(id) => setTab(id as Tab)}
              panelId={panelId}
              items={[
                { id: 'track', label: 'Track' },
                // A program's Epic tab is REPLACED by Phases, never shown
                // alongside it — see the `TABS` comment above.
                initiative.hasChildren
                  ? { id: 'phases', label: 'Phases', count: initiative.children.length }
                  : { id: 'epic', label: EPIC.singular, count: initiative.epic?.total },
                { id: 'sessions', label: 'Sessions', count: initiative.buildSessions?.length },
                { id: 'capabilities', label: CAPABILITY.plural, count: initiative.capabilities.length },
              ]}
            />
            <div role="tabpanel" id={panelId(tab)} aria-labelledby={`tab-${tab}`} className="flex flex-col gap-4">
              {tab === 'track' && <TrackTab initiative={initiative} />}
              {tab === 'epic' && !initiative.hasChildren && <EpicTab initiative={initiative} product={productNode} />}
              {tab === 'phases' && initiative.hasChildren && <PhasesTab initiative={initiative} product={productNode} />}
              {tab === 'sessions' && <SessionsTab initiative={initiative} />}
              {tab === 'capabilities' && (
                <CapabilitiesTab initiative={initiative} onEdit={map ? () => setEditing(true) : null} />
              )}
            </div>
          </div>
        )}
        {/* Nothing to show: either the address is wrong (say so, and offer the
            way back) or the fetch failed (the Banner above carries the reason,
            and the query re-tries on its own interval). Never the loading
            state — a 404 arrives with `isLoading` already false. */}
        {!initiative && (notFound || !isLoading) && (notFound
          ? (
            <EmptyState
              title={`${INITIATIVE.singular} not found`}
              description={`Nothing on the ${ROADMAP.label.toLowerCase()} matches this address. It may have been renamed.`}
              action={
                <Link className="text-accent hover:underline" to={roadmapPath('initiatives')}>
                  Back to the {ROADMAP.label.toLowerCase()}
                </Link>
              }
            />
          )
          : (
            <EmptyState
              title={`Nothing to show for this ${INITIATIVE.singular.toLowerCase()}`}
              description="It will fill in as soon as it loads again."
            />
          ))}
      </PageContent>
      {/* Outside the content region: a modal is not part of the page's scroll.
          The gate moves work between stages, so it asks once before it does. */}
      {initiative && next && (
        <Dialog
          open={confirming}
          onClose={() => setConfirming(false)}
          title={`Advance to ${stageLabel(next)}?`}
          size="sm"
          description={guidance
            ? <ConsequenceList consequence={guidance.actions.advance} tickSeconds={guidance.tickSeconds} />
            : 'This moves the human gate. The stage engine acts on the new stage on its next tick.'}
        >
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
            {/* `lg` is the only size with a 44px touch target — the house rule
                for a primary confirm in a dialog footer. */}
            <Button
              size="lg"
              aria-label="Confirm advance"
              loading={advance.isPending}
              onClick={() => advance.mutate(initiative.slug, { onSettled: () => setConfirming(false) })}
            >
              Advance
            </Button>
          </div>
        </Dialog>
      )}
      {initiative && prev && (
        <Dialog
          open={confirmingBack}
          onClose={() => setConfirmingBack(false)}
          title={`Step back to ${stageLabel(prev)}?`}
          size="sm"
          description={guidance
            ? <ConsequenceList consequence={guidance.actions.step_back} tickSeconds={guidance.tickSeconds} />
            : 'This moves the human gate back one stage. The stage engine re-enters it on its next tick.'}
        >
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirmingBack(false)}>Cancel</Button>
            <Button
              size="lg"
              aria-label="Confirm step back"
              loading={stepBack.isPending}
              onClick={() => stepBack.mutate(initiative.slug, { onSettled: () => setConfirmingBack(false) })}
            >
              Step back
            </Button>
          </div>
        </Dialog>
      )}
      {initiative && blocked && (
        <Dialog
          open={confirmingUnblock}
          onClose={() => setConfirmingUnblock(false)}
          title="Clear the block?"
          size="sm"
          description={guidance
            ? <ConsequenceList consequence={guidance.actions.unblock} tickSeconds={guidance.tickSeconds} />
            : 'This returns the initiative to awaiting-human without moving the gate. '
              + `If the failed materialize left an ${EPIC.singular.toLowerCase()} or sub-issues on GitHub, `
              + 'close them first: a later decomposition creates a fresh set.'}
        >
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirmingUnblock(false)}>Cancel</Button>
            <Button
              size="lg"
              variant="danger"
              aria-label="Confirm unblock"
              loading={unblock.isPending}
              onClick={() => unblock.mutate(initiative.slug, { onSettled: () => setConfirmingUnblock(false) })}
            >
              Unblock
            </Button>
          </div>
        </Dialog>
      )}
      {/* Mounted only while it is open (the RoadmapPage idiom): the form seeds
          from the initiative once, so a dialog left mounted would re-open on an
          abandoned draft — and, because the detail polls, would flip "changed"
          on its own when an external rename moved the seed under it. */}
      {initiative && map && editing && (
        <EditInitiativeDialog
          initiative={initiative}
          products={map.products}
          open
          onClose={() => setEditing(false)}
        />
      )}
    </div>
  )
}
