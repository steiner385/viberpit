// `/roadmap` with nothing open: the whole portfolio in one view. The tiles say
// how big the picture is, and the three tabs answer the three questions asked of
// it — what needs me, what is next, and what exists (finish-line spec §7).
import type { RoadmapMap } from '@/api/roadmap'
import { Tabs } from '@/ui/Tabs'
import { INITIATIVE, PRODUCT, TAB_LABEL, TIER_LABEL } from '@/features/products/vocab'
import { useTabParam } from '../useTabParam'
import { StatTiles } from './StatTiles'
import { NeedsYouList } from './NeedsYouList'
import { needsYouCount } from './needsYou'
import { inFlightCount } from './inFlight'
import { RankedList } from './RankedList'
import { InitiativeList } from './InitiativeList'

const TABS = ['needs-you', 'ranked', 'initiatives'] as const

const panelId = (id: string) => `portfolio-panel-${id}`

export function PortfolioDetail({ map }: { map: RoadmapMap }) {
  const [tab, setTab] = useTabParam(TABS, 'needs-you')
  const products = map.products
  const blocked = products.reduce((n, p) => n + p.counts.blocked, 0)
  // The same population the product and capability levels call "in flight" —
  // the INITIATIVES that are moving or stuck (`inFlight.ts`). Summing the
  // server's `counts.inFlight` here counted issue tiers too, so one label meant
  // two different things one level apart.
  const inFlight = inFlightCount(products)
  // Both the tile and the tab badge count the ROWS the Needs-you list renders,
  // not `counts.needsYou`: that stat excludes the proposal rows (awaiting triage
  // / needs a home) this list renders, so a portfolio whose only outstanding
  // decision is a batch of proposals would read zero above a list with a row in
  // it — it cannot be the badge.
  const needsYou = needsYouCount(products)
  const initiatives = products.flatMap((p) => p.initiatives)

  return (
    <div className="flex flex-col gap-4">
      <StatTiles
        tiles={[
          { label: PRODUCT.plural, value: products.length },
          { label: TIER_LABEL['in-flight'], value: inFlight, tone: 'success', onClick: () => setTab('initiatives') },
          { label: TIER_LABEL.blocked, value: blocked, tone: 'danger', onClick: () => setTab('needs-you') },
          { label: TIER_LABEL['needs-you'], value: needsYou, tone: 'warning', onClick: () => setTab('needs-you') },
        ]}
      />
      <Tabs
        label="Portfolio"
        value={tab}
        onChange={(id) => setTab(id as (typeof TABS)[number])}
        panelId={panelId}
        items={[
          { id: 'needs-you', label: TIER_LABEL['needs-you'], count: needsYou, attention: needsYou > 0 },
          { id: 'ranked', label: TAB_LABEL.ranked },
          { id: 'initiatives', label: `All ${INITIATIVE.plural.toLowerCase()}`, count: initiatives.length },
        ]}
      />
      <div role="tabpanel" id={panelId(tab)} aria-labelledby={`tab-${tab}`}>
        {tab === 'needs-you' && <NeedsYouList products={products} />}
        {tab === 'ranked' && <RankedList />}
        {tab === 'initiatives' && <InitiativeList initiatives={initiatives} />}
      </div>
    </div>
  )
}
