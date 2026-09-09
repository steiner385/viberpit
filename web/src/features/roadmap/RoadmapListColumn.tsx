/**
 * `app.list` for the Roadmap section (finish-line spec §4.3, roadmap spec §6).
 * Map scope: the flattened tree from `treeRows.ts`. Initiatives scope: every
 * initiative through `useItemOrdering` + `GroupedList`, grouped by stage.
 * Region-neutral like `SessionListColumn`: it fills whatever `ContextList`
 * gives it and owns no width, no header and no route.
 */
import { useEffect, useMemo, useState } from 'react'
import type { InitiativeNode, ProductNode, RoadmapMap } from '@/api/roadmap'
import { INITIATIVE_STAGES } from '@/api/sdlc'
import { GroupedList } from '@/components/GroupedList'
import { ListSearchInput } from '@/components/ListSearchInput'
import { SessionListControls } from '@/components/SessionListControls'
import { Banner } from '@/ui/Banner'
import { EmptyState } from '@/ui/EmptyState'
import { Skeleton } from '@/ui/Skeleton'
import { loadGroupCollapseNs, saveGroupCollapseNs } from '@/lib/groupCollapse'
import { useItemOrdering, type OrderingSpec } from '@/session/useSessionOrdering'
import { INITIATIVE, PRODUCT, ROADMAP, stageLabel } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { TIER_RANK } from './roadmapTier'
import { RoadmapScopeSegment } from './RoadmapScopeSegment'
import { InitiativeCard } from './rows/InitiativeCard'
import { TreeRowCard } from './rows/TreeRowCard'
import type { RoadmapScope } from './roadmapNavState'
import { flattenMap, initiativeKey, toggleKey, type TreeRow, type TreeState } from './treeRows'

export interface RoadmapListColumnProps {
  scope: RoadmapScope
  map: RoadmapMap | undefined
  isLoading: boolean
  isError: boolean
  /** Key of the open row (`keyForOpenItem`), or null. */
  selectedKey: string | null
  onOpen: (row: TreeRow) => void
  onOpenInbox: (productSlug: string) => void
}

/**
 * Collapse namespace for the Map tree. The tree's folds are the same fact the
 * lists' group folds are — which keys the user has closed — so they go through
 * `lib/groupCollapse` (guarded read AND guarded write; a private
 * `localStorage.setItem` throws the whole render down in Safari private mode)
 * rather than a second hand-rolled reader. Key: `ccswitch.groupCollapse.roadmap`.
 * The Initiatives scope's group folds are a DIFFERENT list, hence a different
 * namespace (`roadmap-initiatives`, below).
 */
const COLLAPSE_NS = 'roadmap'

/** An initiative plus the product it belongs to — the Initiatives scope flattens
 *  the map, and the row still has to be able to name (and open under) its
 *  product, so the two travel together instead of being re-joined by slug. */
interface InitiativeEntry {
  product: ProductNode
  initiative: InitiativeNode
}

export function RoadmapListColumn({
  scope, map, isLoading, isError, selectedKey, onOpen, onOpenInbox,
}: RoadmapListColumnProps) {
  const products = useMemo(() => map?.products ?? [], [map])
  const entries = useMemo<InitiativeEntry[]>(
    () => products.flatMap((product) => product.initiatives.map((initiative) => ({ product, initiative }))),
    [products],
  )
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="shrink-0 p-2">
        <RoadmapScopeSegment
          scope={scope}
          productCount={products.length}
          initiativeCount={entries.length}
        />
      </div>
      {isError && <Banner tone="danger" title="Could not load the roadmap." className="mx-2" />}
      {scope === 'map' ? (
        <MapScopeBody
          map={map}
          isLoading={isLoading}
          selectedKey={selectedKey}
          onOpen={onOpen}
          onOpenInbox={onOpenInbox}
        />
      ) : (
        <InitiativesScopeBody
          entries={entries}
          isLoading={isLoading}
          selectedKey={selectedKey}
          onOpen={onOpen}
        />
      )}
    </div>
  )
}

type MapScopeBodyProps = Omit<RoadmapListColumnProps, 'scope' | 'isError'>

function MapScopeBody({ map, isLoading, selectedKey, onOpen, onOpenInbox }: MapScopeBodyProps) {
  const [tree, setTree] = useState<TreeState>(() => ({ collapsed: loadGroupCollapseNs(COLLAPSE_NS) }))
  const [query, setQuery] = useState('')
  useEffect(() => {
    saveGroupCollapseNs(COLLAPSE_NS, new Set(tree.collapsed))
  }, [tree])
  const rows = useMemo(() => (map ? flattenMap(map, tree, query) : []), [map, tree, query])

  return (
    <ScopeBody
      controls={
        <ListSearchInput
          value={query}
          onChange={setQuery}
          placeholder={`Search ${ROADMAP.label.toLowerCase()}…`}
          ariaLabel={`Search ${ROADMAP.label.toLowerCase()}`}
        />
      }
    >
      {isLoading && !map && (
        <div role="status" aria-label="Loading roadmap"><Skeleton lines={6} /></div>
      )}
      {map && rows.length === 0 && (
        <EmptyState title={query.trim() ? 'No matches' : PRODUCT.empty} />
      )}
      {rows.length > 0 && (
        <div role="tree" aria-label={ROADMAP.map} className="flex flex-col gap-1">
          {rows.map((row) => (row.kind === 'initiative' ? (
            <InitiativeCard
              key={row.key}
              rowKey={row.key}
              depth={row.depth}
              initiative={row.initiative}
              selected={selectedKey === row.key}
              onOpen={() => onOpen(row)}
            />
          ) : (
            <TreeRowCard
              key={row.key}
              row={row}
              selected={selectedKey === row.key}
              onOpen={onOpen}
              onToggle={(k) => setTree((t) => toggleKey(t, k))}
              onOpenInbox={onOpenInbox}
            />
          )))}
        </div>
      )}
    </ScopeBody>
  )
}

type InitiativeGroupBy = 'stage' | 'product' | 'none'
type InitiativeSortBy = 'tier' | 'title'

const initiativeTitle = (i: InitiativeNode) => i.title ?? i.slug

/** Lifecycle order for the stage grouping; an unknown/absent stage sorts last
 *  rather than first, which a bare `indexOf` (→ -1) would do. */
function stageOrder(stage: string | null): number {
  const i = (INITIATIVE_STAGES as readonly string[]).indexOf(stage ?? '')
  return i < 0 ? INITIATIVE_STAGES.length : i
}

const INITIATIVE_SPEC: OrderingSpec<InitiativeGroupBy, InitiativeSortBy, InitiativeEntry> = {
  namespace: 'roadmap-initiatives',
  groupByValues: ['stage', 'product', 'none'],
  sortByValues: ['tier', 'title'],
  defaultPref: { groupBy: 'stage', sortBy: 'tier' },
  config: {
    idOf: (e) => e.initiative.slug,
    groupings: {
      stage: {
        bucketOf: (e) => ({
          key: e.initiative.stage ?? 'unknown',
          label: stageLabel(e.initiative.stage),
          order: stageOrder(e.initiative.stage),
        }),
      },
      product: {
        bucketOf: (e) => ({ key: e.product.slug, label: e.product.name ?? e.product.slug }),
      },
    },
    sorts: {
      tier: (a, b) => TIER_RANK[a.initiative.tier] - TIER_RANK[b.initiative.tier]
        || initiativeTitle(a.initiative).localeCompare(initiativeTitle(b.initiative)),
      title: (a, b) => initiativeTitle(a.initiative).localeCompare(initiativeTitle(b.initiative)),
    },
  },
}

interface InitiativesScopeBodyProps {
  entries: readonly InitiativeEntry[]
  isLoading: boolean
  selectedKey: string | null
  onOpen: (row: TreeRow) => void
}

function InitiativesScopeBody({ entries, isLoading, selectedKey, onOpen }: InitiativesScopeBodyProps) {
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const visible = useMemo(
    () => (q
      ? entries.filter((e) => `${e.initiative.title ?? ''} ${e.initiative.slug}`.toLowerCase().includes(q))
      : entries),
    [entries, q],
  )
  const ordering = useItemOrdering(visible, INITIATIVE_SPEC)
  return (
    <ScopeBody
      controls={
        <SessionListControls<InitiativeGroupBy, InitiativeSortBy>
          groupBy={ordering.groupBy}
          sortBy={ordering.sortBy}
          onGroupByChange={ordering.setGroupBy}
          onSortByChange={ordering.setSortBy}
          searchQuery={query}
          onSearchChange={setQuery}
          searchPlaceholder={`Search ${INITIATIVE.plural.toLowerCase()}…`}
          searchAriaLabel={`Search ${INITIATIVE.plural.toLowerCase()}`}
          groupOptions={[
            { value: 'stage', label: 'Stage' },
            { value: 'product', label: PRODUCT.singular },
            { value: 'none', label: 'None' },
          ]}
          sortOptions={[
            { value: 'tier', label: 'Attention' },
            { value: 'title', label: 'Title' },
          ]}
        />
      }
    >
      {isLoading && entries.length === 0 && (
        <div role="status" aria-label="Loading roadmap"><Skeleton lines={6} /></div>
      )}
      {!isLoading && visible.length === 0 && (
        <EmptyState title={q ? 'No matches' : `No ${INITIATIVE.plural.toLowerCase()} yet`} />
      )}
      <GroupedList<InitiativeEntry>
        groups={ordering.groups}
        collapsedKeys={ordering.collapsedKeys}
        onToggleCollapse={ordering.toggleCollapse}
        panelRole="list"
        ariaLabel={ROADMAP.initiatives}
        renderRow={(e) => {
          const key = initiativeKey(e.initiative.slug)
          return (
            <InitiativeCard
              key={key}
              rowKey={key}
              depth={0}
              rowRole="listitem"
              initiative={e.initiative}
              selected={selectedKey === key}
              onOpen={() => onOpen({
                kind: 'initiative', key, depth: 0, product: e.product, initiative: e.initiative,
              })}
            />
          )
        }}
      />
    </ScopeBody>
  )
}
