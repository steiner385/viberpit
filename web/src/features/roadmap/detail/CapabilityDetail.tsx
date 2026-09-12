// `/roadmap/:productSlug/capabilities/:capSlug` — one capability, answered the
// way the levels above it are: the tiles say how much work is on it, and the
// tabs answer what it is, what is under it, what is moving, what issues are
// open, and what needs a human (finish-line spec §7).
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import { Tabs } from '@/ui/Tabs'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { EmptyState } from '@/ui/EmptyState'
import { fileUrl } from '@/lib/githubUrl'
import { CAPABILITY, INITIATIVE, ISSUE, PRODUCT, TIER_LABEL, capabilityStateLabel } from '@/features/products/vocab'
import { useTabParam } from '../useTabParam'
import { capabilityPath } from '../roadmapNavState'
import { findCapability, openMapChanges } from '../lookup'
import { TIER_TONE, tierLabel } from '../roadmapTier'
import { StatTiles } from './StatTiles'
import { NeedsYouList } from './NeedsYouList'
import { needsYouCount } from './needsYou'
import { inFlightInitiatives } from './inFlight'
import { InitiativeList } from './InitiativeList'
import { LeafList } from './LeafList'
import { directOpenIssues } from './directOpenIssues'
import { EditCapabilityDialog } from './EditCapabilityDialog'
import { NewCapabilityDialog } from './NewCapabilityDialog'
import { CapabilityLinksEditor } from './CapabilityLinksEditor'

const TABS = ['overview', 'children', 'in-flight', 'issues', 'needs-you'] as const

const panelId = (id: string) => `capability-panel-${id}`

/** The capability's own initiatives: the ones homed here, plus the ones the
 *  server says merely touch it — both are work a human would expect to find
 *  under this capability. */
function initiativesOn(product: ProductNode, capability: CapabilityNode) {
  const touching = new Set(capability.initiatives)
  return product.initiatives.filter(
    (i) => i.primaryCapability === capability.slug || touching.has(i.slug),
  )
}

export function CapabilityDetail({ product, capability, onNewInitiative, primaryRepo, projectError }: {
  product: ProductNode
  capability: CapabilityNode
  onNewInitiative: () => void
  /** The repo the feature doc is committed in; without it there is no doc link. */
  primaryRepo?: string
  /** The product fetch that would have supplied `primaryRepo` failed. Without
   *  this the missing doc link is indistinguishable from "there is no doc". */
  projectError?: boolean
}) {
  const [tab, setTab] = useTabParam(TABS, 'overview')
  const [editing, setEditing] = useState(false)
  const [addingChild, setAddingChild] = useState(false)
  const mine = initiativesOn(product, capability)
  // The product narrowed to this capability's initiatives, so "in flight" is
  // read by the same module the portfolio and product levels read it with.
  // Leaf-only (`inFlight.ts`): a program itself is never counted, only its
  // phases are.
  const inFlight = inFlightInitiatives([{ ...product, initiatives: mine }])
  // The In-flight tab still shows a phase's program above it for context — it
  // is just never part of the count above. `InitiativeList` nests purely
  // structurally, so passing the program alongside the one phase that
  // qualifies is enough; it does not need the rest of that program's phases.
  //
  // INTERLEAVED, not prepended: a program is inserted immediately before its
  // first in-flight phase, keeping `inFlight`'s own map order for everything
  // else — prepending every qualifying program ahead of the whole list would
  // jump it above leaves that precede it in map order.
  const shownPrograms = new Set<string>()
  const inFlightWithProgramContext = inFlight.flatMap((f) => {
    const program = f.parentInitiative && !shownPrograms.has(f.parentInitiative)
      ? mine.find((i) => i.hasChildren && i.slug === f.parentInitiative)
      : undefined
    if (program) shownPrograms.add(program.slug)
    return program ? [program, f] : [f]
  })
  const needsYou = needsYouCount([product], capability)
  const c = capability.counts
  // The issues on THIS capability, not on the branch under it: the Issues panel
  // lists direct children only, so the rollup would badge issues it cannot show.
  const openIssues = directOpenIssues(capability)
  // Pinned at the SHA the doc was read at, so the link keeps pointing at the
  // words this page is showing even after the branch moves on.
  const docHref = primaryRepo && capability.docPath && capability.commitSha
    ? fileUrl(primaryRepo, capability.commitSha, capability.docPath)
    : null
  const parent = capability.parentSlug ? findCapability(product, capability.parentSlug) : null
  // Every edit below commits to the product's map-changes branch, so while that
  // PR is open and this capability is on it, what the page shows is what the PR
  // proposes — not what master says (R8c-3).
  // The same "is there a live PR?" reading the product's card uses, so the note
  // and the card can never disagree about whether the pointer is still open.
  const pending = capability.prNumber != null ? openMapChanges(product) : null

  return (
    <div className="flex flex-col gap-4">
      <StatTiles
        tiles={[
          { label: `Open ${ISSUE.plural.toLowerCase()}`, value: openIssues, onClick: () => setTab('issues') },
          // The initiatives the In-flight tab LISTS, the same reading as the
          // product level (`counts.inFlight` also rolls up issue tiers).
          { label: TIER_LABEL['in-flight'], value: inFlight.length, tone: 'success', onClick: () => setTab('in-flight') },
          { label: TIER_LABEL.blocked, value: c.blocked, tone: 'danger', onClick: () => setTab('needs-you') },
          { label: TIER_LABEL['needs-you'], value: needsYou, tone: 'warning', onClick: () => setTab('needs-you') },
        ]}
      />
      <Tabs
        label={capability.title ?? capability.slug}
        value={tab}
        onChange={(id) => setTab(id as (typeof TABS)[number])}
        panelId={panelId}
        items={[
          { id: 'overview', label: 'Overview' },
          { id: 'children', label: `Sub-${CAPABILITY.plural.toLowerCase()}`, count: capability.children.length },
          { id: 'in-flight', label: TIER_LABEL['in-flight'], count: inFlight.length },
          { id: 'issues', label: ISSUE.plural, count: openIssues },
          { id: 'needs-you', label: TIER_LABEL['needs-you'], count: needsYou, attention: needsYou > 0 },
        ]}
      />
      <div role="tabpanel" id={panelId(tab)} aria-labelledby={`tab-${tab}`} className="flex flex-col gap-4">
        {tab === 'overview' && (
          <section className="flex flex-col gap-2 text-sm">
            <NeedsYouList products={[product]} capability={capability} overview />
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={capability.state === 'live' ? 'success' : 'info'}>
                {capabilityStateLabel(capability.state)}
              </Badge>
              <Badge tone={TIER_TONE[capability.tier]}>{tierLabel(capability.tier)}</Badge>
              {/* A capability can exist on the map before its doc is merged. */}
              {capability.prNumber != null && <Badge tone="info">proposed in PR #{capability.prNumber}</Badge>}
              {docHref && (
                <a href={docHref} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                  feature doc ↗
                </a>
              )}
              {/* The doc lives in the product's primary repo, so a failed
                  product fetch takes the link with it. Said out loud, because a
                  silently absent link reads as "this capability has no doc". */}
              {!docHref && projectError && capability.docPath && (
                <span role="status" className="text-xs text-ink-muted">
                  Doc link unavailable — could not load the {PRODUCT.singular.toLowerCase()}
                </span>
              )}
            </div>
            {capability.summary
              ? <p className="text-ink">{capability.summary}</p>
              : <p className="text-ink-muted">No summary in the feature doc yet.</p>}
            {parent && (
              <p className="text-xs text-ink-muted">
                Part of{' '}
                <Link className="text-accent hover:underline" to={capabilityPath(product.slug, parent.slug)}>
                  {parent.title ?? parent.slug}
                </Link>
              </p>
            )}
            {pending && (
              <p className="text-xs text-ink-muted">
                Changes pending in{' '}
                {pending.prUrl
                  ? (
                    <a href={pending.prUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                      PR #{pending.prNumber}
                    </a>
                  )
                  : `PR #${pending.prNumber}`}
              </p>
            )}
            <CapabilityLinksEditor product={product} capability={capability} />
            {/* The capability is the home an initiative wants: starting one here
                pre-fills it rather than asking the user to pick it again. */}
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>Edit</Button>
              <Button size="sm" variant="ghost" onClick={() => setAddingChild(true)}>
                + Sub-{CAPABILITY.singular.toLowerCase()}
              </Button>
              <Button size="sm" variant="ghost" onClick={onNewInitiative}>
                + {INITIATIVE.singular} here
              </Button>
            </div>
          </section>
        )}
        {tab === 'children' && (capability.children.length === 0
          ? <EmptyState title={`No sub-${CAPABILITY.plural.toLowerCase()}`} />
          : (
            <ul className="flex flex-col gap-1">
              {capability.children.map((ch) => (
                <li key={ch.id} className="rounded-md border border-edge bg-surface p-3 text-sm">
                  <Link to={capabilityPath(product.slug, ch.slug)} className="font-medium text-ink hover:underline">
                    {ch.title ?? ch.slug}
                  </Link>
                  <span className="ml-2 text-xs text-ink-muted">
                    {ch.counts.openIssues} open · {tierLabel(ch.tier)}
                  </span>
                </li>
              ))}
            </ul>
          ))}
        {tab === 'in-flight' && <InitiativeList initiatives={inFlightWithProgramContext} />}
        {tab === 'issues' && <LeafList scope={{ capability: capability.id }} product={product} />}
        {/* `repos={[]}` is a real, empty scope: incidents are filed against a
            repository, which belongs to the product, so a capability shows none
            rather than repeating its product's whole incident queue. */}
        {tab === 'needs-you' && <NeedsYouList products={[product]} capability={capability} repos={[]} />}
      </div>
      {/* Mounted only while open (the RoadmapPage idiom): both forms seed from
          the capability once, so one left mounted would re-open on an abandoned
          draft — and, because the map polls, would flip "changed" on its own
          when an edit from elsewhere moved the seed under it. */}
      {editing && (
        <EditCapabilityDialog product={product} capability={capability} open onClose={() => setEditing(false)} />
      )}
      {addingChild && (
        <NewCapabilityDialog product={product} parentSlug={capability.slug} open onClose={() => setAddingChild(false)} />
      )}
    </div>
  )
}
