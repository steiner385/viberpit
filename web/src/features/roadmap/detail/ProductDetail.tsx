// `/roadmap/:productSlug` — one product, answered the same way the portfolio is:
// the tiles say how big it is, and the tabs answer what it is, what is moving,
// what needs me, what is next, and what it waits on (finish-line spec §7).
import { Link } from 'react-router-dom'
import type { ProductNode } from '@/api/roadmap'
import { useProject } from '@/api/hooks'
import { Banner } from '@/ui/Banner'
import { Tabs } from '@/ui/Tabs'
import { Skeleton } from '@/ui/Skeleton'
import { InitiativeTrack } from '@/initiatives/InitiativeTrack'
import { OnboardingWizard } from '@/projects/OnboardingWizard'
import { DependencyPanel } from '@/projects/DependencyPanel'
import { CAPABILITY, INITIATIVE, ISSUE, TIER_LABEL } from '@/features/products/vocab'
import { useTabParam } from '../useTabParam'
import { initiativePath } from '../roadmapNavState'
import { IN_FLIGHT_TIERS } from '../roadmapTier'
import { StatTiles } from './StatTiles'
import { NeedsYouList } from './NeedsYouList'
import { needsYouCount } from './needsYou'
import { RankedList } from './RankedList'
import { InitiativeList } from './InitiativeList'

const TABS = ['overview', 'in-flight', 'needs-you', 'ranked', 'dependencies'] as const

const panelId = (id: string) => `product-panel-${id}`

export function ProductDetail({ product }: { product: ProductNode }) {
  const [tab, setTab] = useTabParam(TABS, 'overview')
  // Shares `QK.project(slug)` with the header and the new-initiative dialog, so
  // the three of them are one fetch.
  const { data: project, isLoading, isError } = useProject(product.slug)
  // `undefined` until the project ARRIVES: an empty array is a real scope that
  // hides every incident, and "no repos yet" must not be indistinguishable from
  // "the repo list has not loaded".
  const repos = project ? project.repos.map((r) => r.repo).filter((r): r is string => !!r) : undefined
  const inFlight = product.initiatives.filter((i) => IN_FLIGHT_TIERS.has(i.tier))
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
          { label: TIER_LABEL['in-flight'], value: inFlight.length, tone: 'success' },
          { label: TIER_LABEL.blocked, value: c.blocked, tone: 'danger' },
          { label: TIER_LABEL['needs-you'], value: needsYou, tone: 'warning' },
        ]}
      />
      <Tabs
        label={product.name ?? product.slug}
        value={tab}
        onChange={(id) => setTab(id as (typeof TABS)[number])}
        panelId={panelId}
        items={[
          { id: 'overview', label: 'Overview' },
          { id: 'in-flight', label: TIER_LABEL['in-flight'], count: inFlight.length },
          { id: 'needs-you', label: TIER_LABEL['needs-you'], count: needsYou, attention: needsYou > 0 },
          { id: 'ranked', label: 'Ranked' },
          { id: 'dependencies', label: 'Dependencies' },
        ]}
      />
      <div role="tabpanel" id={panelId(tab)} aria-labelledby={`tab-${tab}`} className="flex flex-col gap-4">
        {tab === 'overview' && (
          <>
            {isLoading && !project
              ? <Skeleton lines={3} />
              : project && (
                <section className="flex flex-col gap-2 text-sm">
                  {project.description && <p className="text-ink">{project.description}</p>}
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
              )}
            {/* Renders nothing once the product is live. */}
            <OnboardingWizard slug={product.slug} />
            <section aria-label={`${INITIATIVE.plural} in flight`} className="flex flex-col gap-3">
              {inFlight.length === 0
                ? <p className="text-sm text-ink-muted">Nothing in flight.</p>
                : inFlight.map((i) => (
                  // `prdash-root` scopes plotroom.css for the track; it goes when
                  // finish-line row 5 moves the track onto tokens.
                  <div key={i.slug} className="prdash-root rounded-md border border-edge bg-surface p-3">
                    <Link to={initiativePath(i.slug)} className="font-medium text-ink hover:underline">
                      {i.title ?? i.slug}
                    </Link>
                    <InitiativeTrack stage={i.stage} status={i.status} epic={i.epic} />
                  </div>
                ))}
            </section>
          </>
        )}
        {tab === 'in-flight' && <InitiativeList initiatives={inFlight} />}
        {tab === 'needs-you' && <NeedsYouList products={[product]} repos={repos} />}
        {tab === 'ranked' && <RankedList productSlug={product.slug} />}
        {tab === 'dependencies' && <DependencyPanel slug={product.slug} />}
      </div>
    </div>
  )
}
