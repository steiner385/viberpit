// `/roadmap/:productSlug/capabilities/:capSlug` — one capability, answered the
// way the levels above it are: the tiles say how much work is on it, and the
// tabs answer what it is, what is under it, what is moving, what issues are
// open, and what needs a human (finish-line spec §7).
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
import { findCapability } from '../lookup'
import { TIER_TONE, tierLabel } from '../roadmapTier'
import { StatTiles } from './StatTiles'
import { NeedsYouList } from './NeedsYouList'
import { needsYouCount } from './needsYou'
import { inFlightInitiatives } from './inFlight'
import { InitiativeList } from './InitiativeList'
import { LeafList } from './LeafList'
import { directOpenIssues } from './directOpenIssues'

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
  const mine = initiativesOn(product, capability)
  // The product narrowed to this capability's initiatives, so "in flight" is
  // read by the same module the portfolio and product levels read it with.
  const inFlight = inFlightInitiatives([{ ...product, initiatives: mine }])
  // The product, narrowed to this capability: the Needs-you panel below reads
  // its rows from this, and the tile and badge count the same object, so the
  // number above the list can never disagree with the list.
  // The proposal counts are zeroed deliberately — awaiting triage and "needs a
  // home" are product-wide decisions, and a capability that claimed them would
  // repeat the same work on every capability page of the product.
  const scoped: ProductNode = {
    ...product,
    initiatives: mine.filter((i) => i.tier === 'needs-you'),
    counts: { ...product.counts, awaitingTriage: 0, needsHome: 0 },
  }
  const needsYou = needsYouCount([scoped])
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

  return (
    <div className="flex flex-col gap-4">
      <StatTiles
        tiles={[
          { label: `Open ${ISSUE.plural.toLowerCase()}`, value: openIssues },
          // The initiatives the In-flight tab LISTS, the same reading as the
          // product level (`counts.inFlight` also rolls up issue tiers).
          { label: TIER_LABEL['in-flight'], value: inFlight.length, tone: 'success' },
          { label: TIER_LABEL.blocked, value: c.blocked, tone: 'danger' },
          { label: TIER_LABEL['needs-you'], value: needsYou, tone: 'warning' },
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
            {capability.links.length > 0 && (
              <ul className="flex flex-wrap gap-2" aria-label="Linked capabilities">
                {capability.links.map((l) => (
                  <li key={`${l.kind}:${l.projectSlug}:${l.featureSlug}`}>
                    <Link
                      to={capabilityPath(l.projectSlug, l.featureSlug)}
                      className="rounded-pill border border-edge px-2 py-0.5 text-xs text-ink-muted hover:text-ink"
                    >
                      {l.kind.replace(/_/g, ' ')} · {l.projectSlug}/{l.featureSlug}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
            {/* The capability is the home an initiative wants: starting one here
                pre-fills it rather than asking the user to pick it again. */}
            <div>
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
        {tab === 'in-flight' && <InitiativeList initiatives={inFlight} />}
        {tab === 'issues' && <LeafList scope={{ capability: capability.id }} />}
        {/* `repos={[]}` is a real, empty scope: incidents are filed against a
            repository, which belongs to the product, so a capability shows none
            rather than repeating its product's whole incident queue. */}
        {tab === 'needs-you' && <NeedsYouList products={[scoped]} repos={[]} />}
      </div>
    </div>
  )
}
