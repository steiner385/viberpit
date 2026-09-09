// `/initiatives/:initiativeSlug` — the initiative level, and the only page in
// the Roadmap section with its own prefix (roadmap spec §4). It answers the
// four questions a human has about one initiative: where it is on the track,
// what its epic is doing, which sessions built it, and which capabilities it
// touches — and it carries the human gate (Advance), which is a two-step so a
// mis-tap never moves the stage.
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useAdvanceInitiative, useInitiative, useRoadmapMap } from '@/api/hooks'
import { ApiRequestError } from '@/api/client'
import type { Initiative, InitiativeArtifact } from '@/api/sdlc'
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
  initiativeStatusLabel, nextActionLabel, nextStage, reasonLabel, stageLabel,
} from '@/features/products/vocab'
import { capabilityPath, productPath, roadmapPath } from './roadmapNavState'
import { TIER_TONE } from './roadmapTier'
import { useTabParam } from './useTabParam'
import { StatTiles } from './detail/StatTiles'

const TABS = ['track', 'epic', 'sessions', 'capabilities'] as const
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

function TrackTab({ initiative }: { initiative: Initiative }) {
  const next = initiative.nextAction
  // The server sends artifacts in ASCENDING order (intent, spec, plan), so the
  // LAST committed one is the latest — `find` would link the intent commit.
  const newestCommitted = initiative.artifacts.findLast((a) => a.commitSha)
  return (
    // `prdash-root` scopes plotroom.css for the track; it goes when finish-line
    // row 5 moves the track onto tokens (same note as ProductDetail).
    <div className="prdash-root flex flex-col gap-3">
      <InitiativeTrack stage={initiative.stage} status={initiative.status} epic={initiative.epic} />
      {next && (
        <p className="text-sm text-ink-muted">
          {nextActionLabel(next.type)}{next.reason ? ` — ${next.reason}` : ''}
        </p>
      )}
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

function EpicTab({ initiative }: { initiative: Initiative }) {
  const epic = initiative.epic
  if (!epic) {
    return (
      <EmptyState
        title={`No ${EPIC.singular.toLowerCase()} yet`}
        description="One appears when the plan is materialized into GitHub issues."
      />
    )
  }
  const children = epic.children ?? []
  return (
    <div className="flex flex-col gap-3">
      <a href={epic.url} target="_blank" rel="noreferrer" className="text-sm text-accent hover:underline">
        {epic.ref} ↗
      </a>
      <StatTiles
        tiles={[
          { label: 'Done', value: epic.done, tone: 'success' },
          { label: 'Total', value: epic.total },
          { label: 'Blocked', value: epic.blocked, tone: 'danger' },
          { label: 'Needs human', value: epic.needsHuman, tone: 'warning' },
        ]}
      />
      {children.length === 0
        ? <p className="text-sm text-ink-muted">No sub-issues under it yet.</p>
        : (
          <ul className="flex flex-col gap-1" aria-label="Sub-issues">
            {children.map((c) => (
              <li key={c.ref} className="flex items-center gap-2 rounded-md border border-edge bg-surface p-2 text-sm">
                <Badge tone={c.state === 'closed' ? 'success' : 'neutral'}>{c.status ?? c.state ?? '—'}</Badge>
                <span className="truncate text-ink">{c.title ?? c.ref}</span>
                <span className="ml-auto shrink-0 text-xs text-ink-muted">{c.ref}</span>
              </li>
            ))}
          </ul>
        )}
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

function CapabilitiesTab({ initiative }: { initiative: Initiative }) {
  const { projectSlug } = initiative
  if (!projectSlug || initiative.capabilities.length === 0) {
    return (
      <EmptyState
        title={`No ${CAPABILITY.plural.toLowerCase()} linked`}
        description={`Triage gives an ${INITIATIVE.singular.toLowerCase()} its home on the map.`}
      />
    )
  }
  return (
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
  )
}

export function InitiativePage() {
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
  const [tab, setTab] = useTabParam(TABS, 'track')
  const [confirming, setConfirming] = useState(false)

  // The slug is the fallback title, so a deep link names something rather than
  // rendering a blank heading while the detail is in flight.
  const title = initiative?.title ?? initiativeSlug
  const node = map?.products.flatMap((p) => p.initiatives).find((i) => i.slug === initiativeSlug)
  const reason = reasonLabel(node?.reason)
  const next = nextStage(initiative?.stage)

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
  const productName = productSlug
    ? (map?.products.find((p) => p.slug === productSlug)?.name ?? productSlug)
    : null

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
                {initiative.repo && <span className="text-xs text-ink-muted">{initiative.repo}</span>}
              </div>
            )}
          </div>
        }
        actions={initiative && (
          // Disabled rather than absent at the end of the lifecycle: the gate is
          // the page's one action, and a header that loses its only button reads
          // as a page that failed to load.
          <Button
            size="sm"
            disabled={!next || advance.isPending}
            onClick={() => setConfirming(true)}
            title={next ? `Move the gate to ${stageLabel(next)}` : 'Already at the final stage'}
          >
            {next ? `Advance → ${stageLabel(next)}` : 'Complete'}
          </Button>
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
        {initiative && (
          <div className="flex flex-col gap-4">
            <Tabs
              label={title}
              value={tab}
              onChange={(id) => setTab(id as Tab)}
              panelId={panelId}
              items={[
                { id: 'track', label: 'Track' },
                { id: 'epic', label: EPIC.singular, count: initiative.epic?.total },
                { id: 'sessions', label: 'Sessions', count: initiative.buildSessions?.length },
                { id: 'capabilities', label: CAPABILITY.plural, count: initiative.capabilities.length },
              ]}
            />
            <div role="tabpanel" id={panelId(tab)} aria-labelledby={`tab-${tab}`} className="flex flex-col gap-4">
              {tab === 'track' && <TrackTab initiative={initiative} />}
              {tab === 'epic' && <EpicTab initiative={initiative} />}
              {tab === 'sessions' && <SessionsTab initiative={initiative} />}
              {tab === 'capabilities' && <CapabilitiesTab initiative={initiative} />}
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
          description="This moves the human gate. The stage engine acts on the new stage on its next tick."
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
    </div>
  )
}
