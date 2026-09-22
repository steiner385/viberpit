// `/roadmap/:productSlug` — one product, on the page-tabs standard
// (`docs/PAGE-TABS.md`): Overview · In flight · Needs you · Ranked ·
// Dependencies · Automation, plus a 7th, Capabilities, as a recorded
// exception (docs/PAGE-TABS.md §8). The tiles say how big it is; the tabs
// answer what it is, what is moving, what needs me, what is next, what it
// waits on, what it is configured to do, and (Capabilities) what it is made
// of and how that's managed.
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ProductNode } from '@/api/roadmap'
import { useProject } from '@/api/hooks'
import { Banner } from '@/ui/Banner'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Tabs } from '@/ui/Tabs'
import { Skeleton } from '@/ui/Skeleton'
import { InitiativeTrack } from '@/initiatives/InitiativeTrack'
import { OnboardingWizard } from '@/projects/OnboardingWizard'
import { DependencyPanel } from '@/projects/DependencyPanel'
import { CAPABILITY, INITIATIVE, ISSUE, PRODUCT, TAB_LABEL, TIER_LABEL } from '@/features/products/vocab'
import { CapabilitiesTab } from '@/features/products/CapabilitiesTab'
import { useTabParam } from '../useTabParam'
import { initiativePath } from '../roadmapNavState'
import { programTitleOf } from '../lookup'
import { StatTiles } from './StatTiles'
import { NeedsYouList } from './NeedsYouList'
import { needsYouCount } from './needsYou'
import { inFlightInitiatives } from './inFlight'
import { RankedList } from './RankedList'
import { InitiativeList } from './InitiativeList'
import { ProductEdgesEditor } from './ProductEdgesEditor'
import { NewCapabilityDialog } from './NewCapabilityDialog'
import { EditProductDialog } from './EditProductDialog'
import { MapChangesCard } from './MapChangesCard'
import { AutomationPolicyCard } from './AutomationPolicyCard'
import { RoadmapHealthCard } from './RoadmapHealthCard'

const TABS = ['overview', 'in-flight', 'needs-you', 'ranked', 'dependencies', 'automation', 'capabilities'] as const

/** The Overview previews this many in-flight initiatives; the In-flight tab
 *  lists them all (`docs/PAGE-TABS.md` §5). */
const OVERVIEW_PREVIEW_ROWS = 3

const panelId = (id: string) => `product-panel-${id}`

export function ProductDetail({ product }: { product: ProductNode }) {
  const [tab, setTab] = useTabParam(TABS, 'overview')
  const [addingCapability, setAddingCapability] = useState(false)
  const [editing, setEditing] = useState(false)
  // Shares `QK.project(slug)` with the header and the new-initiative dialog, so
  // the three of them are one fetch.
  const { data: project, isLoading, isError } = useProject(product.slug)
  // `undefined` until the project ARRIVES: an empty array is a real scope that
  // hides every incident, and "no repos yet" must not be indistinguishable from
  // "the repo list has not loaded".
  const repos = project ? project.repos.map((r) => r.repo).filter((r): r is string => !!r) : undefined
  // The incident queue's scope. `undefined` means "every repo" to IncidentQueue,
  // so passing it through while the project is in flight showed OTHER products'
  // incidents on this product's page. A failed fetch resolves to the empty
  // scope: the Banner above already says the repositories are missing, and an
  // unscoped queue is worse than none.
  const incidentScope: readonly string[] | null = repos ?? (isError ? [] : null)
  const inFlight = inFlightInitiatives([product])
  // A phase's program title, computed once per product — `inFlightInitiatives`
  // is leaf-only, so a phase here has no program row of its own to nest under
  // structurally; this is how it still names the program it belongs to.
  const programTitleFor = useMemo(() => programTitleOf(product), [product])
  // Same number as the portfolio's row for this product: `counts.needsYou`
  // excludes the proposal rows (awaiting triage / needs a home) that this list
  // renders, so it cannot be the badge.
  const needsYou = needsYouCount([product])
  const c = product.counts

  return (
    <div className="flex flex-col gap-4">
      {/* Above the tabs, not inside Overview: the failure costs the repo list on
          every tab, and it must not disappear when a tab changes. The tree below
          comes from the map, so it stays. */}
      {isError && (
        <Banner tone="danger" title="Could not load this product.">
          Its repositories and description are missing; everything else is from the roadmap map.
        </Banner>
      )}
      <StatTiles
        tiles={[
          { label: CAPABILITY.plural, value: c.capabilities },
          // The initiatives the In-flight tab LISTS, not `counts.inFlight`: the
          // server's rollup also counts issue tiers, so the tile would read a
          // different number from the tab badge beside it. (The issue-level
          // count belongs to the capability tiles.)
          { label: TIER_LABEL['in-flight'], value: inFlight.length, tone: 'success', onClick: () => setTab('in-flight') },
          { label: TIER_LABEL.blocked, value: c.blocked, tone: 'danger', onClick: () => setTab('needs-you') },
          { label: TIER_LABEL['needs-you'], value: needsYou, tone: 'warning', onClick: () => setTab('needs-you') },
        ]}
      />
      <Tabs
        label={product.name ?? product.slug}
        value={tab}
        onChange={(id) => setTab(id as (typeof TABS)[number])}
        panelId={panelId}
        items={[
          { id: 'overview', label: TAB_LABEL.overview },
          { id: 'in-flight', label: TIER_LABEL['in-flight'], count: inFlight.length },
          { id: 'needs-you', label: TIER_LABEL['needs-you'], count: needsYou, attention: needsYou > 0 },
          { id: 'ranked', label: TAB_LABEL.ranked },
          { id: 'dependencies', label: TAB_LABEL.dependencies },
          { id: 'automation', label: TAB_LABEL.automation },
          { id: 'capabilities', label: CAPABILITY.plural },
        ]}
      />
      <div role="tabpanel" id={panelId(tab)} aria-labelledby={`tab-${tab}`} className="flex flex-col gap-4">
        {tab === 'overview' && (
          <>
            {/* Below the tiles above: a notice, not a tile, so it does not
                compete with the counts for the top of the page. */}
            <NeedsYouList products={[product]} overview />
            {isLoading && !project
              ? <Skeleton lines={3} />
              : project && (
                <>
                  <section className="flex flex-col gap-2 text-sm">
                    <div className="flex items-start justify-between gap-2">
                      {project.description
                        ? <p className="text-ink">{project.description}</p>
                        : <span />}
                      <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
                        Edit
                      </Button>
                    </div>
                    {project.repos.length > 0 && (
                      <ul className="flex flex-wrap gap-2" aria-label="Repositories">
                        {project.repos.map((r) => (
                          <li key={r.id} className="rounded-pill border border-edge px-2 py-0.5 text-xs text-ink-muted">
                            {r.repo}{r.isPrimary ? ' · primary' : ''}{r.backfillDone ? '' : ' · backfilling'}
                          </li>
                        ))}
                      </ul>
                    )}
                    <p className="text-xs text-ink-muted">
                      {project.workItemCount} {ISSUE.plural.toLowerCase()} mirrored
                    </p>
                  </section>
                  {/* Mounted only while open, like `NewCapabilityDialog` below — each
                      opening starts from the project's current values rather than
                      whatever a previous, abandoned edit left the fields holding. */}
                  {editing && <EditProductDialog project={project} open onClose={() => setEditing(false)} />}
                </>
              )}
            {/* On the Overview, not in the page header (R8c-3): the header
                carries the breadcrumbs and "+ Initiative", and a capability is
                a change to the MAP — it opens the product's map-changes PR. */}
            <div>
              <Button size="sm" variant="ghost" onClick={() => setAddingCapability(true)}>
                + {CAPABILITY.singular}
              </Button>
            </div>
            {/* The wizard owns the map-PR link until the product is live (its
                `await_map_pr` action links the same PR); showing the card too
                would put two links to one PR in the same "what is the map
                waiting on" notice (docs/PAGE-TABS.md §5). Once live, the
                wizard has already unmounted itself, so the card is the only
                one left to show a late-arriving pointer. Renders nothing
                unless the product has an open map-changes PR (R8c-3). */}
            {(!project || project.onboardingState === 'live') && <MapChangesCard product={product} />}
            {/* Renders nothing once the product is live. */}
            <OnboardingWizard slug={product.slug} />
            <section aria-label={`${INITIATIVE.plural} in flight`} className="flex flex-col gap-3">
              {inFlight.length === 0
                ? <p className="text-sm text-ink-muted">Nothing in flight.</p>
                : inFlight.slice(0, OVERVIEW_PREVIEW_ROWS).map((i) => {
                  const programTitle = programTitleFor(i)
                  return (
                    <div key={i.slug} className="rounded-md border border-edge bg-surface p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <Link to={initiativePath(i.slug)} className="font-medium text-ink hover:underline">
                          {i.title ?? i.slug}
                        </Link>
                        {programTitle && (
                          <Badge tone="neutral" title={`Part of ${programTitle}`} aria-label={`Part of ${programTitle}`}>
                            {programTitle}
                          </Badge>
                        )}
                      </div>
                      <InitiativeTrack stage={i.stage} status={i.status} epic={i.epic} />
                    </div>
                  )
                })}
              {inFlight.length > OVERVIEW_PREVIEW_ROWS && (
                <div>
                  <Button size="sm" variant="ghost" onClick={() => setTab('in-flight')}>
                    See all {inFlight.length} in flight
                  </Button>
                </div>
              )}
            </section>
          </>
        )}
        {tab === 'in-flight' && <InitiativeList initiatives={inFlight} programTitleFor={programTitleFor} />}
        {tab === 'needs-you' && (
          <>
            {/* Self-hides when there is nothing to say — a hygiene issue is
                itself a "needs a person" question (docs/PAGE-TABS.md §5), not
                an Overview summary block. */}
            <RoadmapHealthCard productSlug={product.slug} />
            {incidentScope
              ? <NeedsYouList products={[product]} repos={incidentScope} />
              : <Skeleton lines={3} />}
          </>
        )}
        {tab === 'ranked' && <RankedList productSlug={product.slug} />}
        {tab === 'dependencies' && (
          <div className="flex flex-col gap-4">
            <section aria-labelledby="deps-products">
              <h3 id="deps-products" className="mb-2 text-xs font-medium uppercase text-ink-muted">Between {PRODUCT.plural.toLowerCase()}</h3>
              <ProductEdgesEditor product={product} />
            </section>
            <section aria-labelledby="deps-issues">
              <h3 id="deps-issues" className="mb-2 text-xs font-medium uppercase text-ink-muted">Between {ISSUE.plural.toLowerCase()}</h3>
              <DependencyPanel slug={product.slug} />
            </section>
          </div>
        )}
        {tab === 'automation' && (isLoading && !project
          ? <Skeleton lines={3} />
          : project && <AutomationPolicyCard product={product} project={project} />)}
        {tab === 'capabilities' && (
          <CapabilitiesTab product={product} capabilities={product.capabilities ?? []} />
        )}
      </div>
      {/* Mounted only while open (the RoadmapPage idiom), so each opening starts
          on a blank form rather than on whatever the last one was abandoned
          holding. */}
      {addingCapability && <NewCapabilityDialog product={product} open onClose={() => setAddingCapability(false)} />}
    </div>
  )
}
