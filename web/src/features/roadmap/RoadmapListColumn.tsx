/**
 * `app.list` for the Roadmap section (finish-line spec §4.3, roadmap spec §6).
 * Map scope: the flattened tree from `treeRows.ts`. Initiatives scope: every
 * initiative through `useItemOrdering` + `GroupedList`, grouped by stage.
 * Inbox scope: the pending triage proposals (`inbox/InboxScopeBody`), which is
 * where the old standalone Triage page's queue and key map now live.
 * Region-neutral like `SessionListColumn`: it fills whatever `ContextList`
 * gives it and owns no width, no header and no route.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { InitiativeNode, ProductNode, RoadmapMap } from '@/api/roadmap'
import { INITIATIVE_STAGES } from '@/api/sdlc'
import { GroupedList } from '@/ui/list/GroupedList'
import { ListSearchInput } from '@/ui/list/ListSearchInput'
import { SessionListControls } from '@/features/sessions/list/SessionListControls'
import { Banner } from '@/ui/Banner'
import { EmptyState } from '@/ui/EmptyState'
import { Skeleton } from '@/ui/Skeleton'
import { loadGroupCollapseNs, saveGroupCollapseNs } from '@/lib/groupCollapse'
import { useItemOrdering, type OrderingSpec } from '@/session/useSessionOrdering'
import { INITIATIVE, PRODUCT, ROADMAP, stageLabel } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { InboxScopeBody, type InboxScopeBodyProps } from './inbox/InboxScopeBody'
import { programTitleOf } from './lookup'
import { TIER_RANK } from './roadmapTier'
import { RoadmapScopeSegment } from './RoadmapScopeSegment'
import { InitiativeCard } from './rows/InitiativeCard'
import { TreeRowCard } from './rows/TreeRowCard'
import type { RoadmapScope } from './roadmapNavState'
import { flattenMap, initiativeKey, setCollapsed, toggleKey, type TreeRow, type TreeState } from './treeRows'
import { useTreeKeys } from './useTreeKeys'

/**
 * The Inbox scope's half of the column's props. `selectedKey` is NOT here — the
 * column already carries the one open-item key for all three scopes and hands
 * it down, so a proposal cannot end up highlighted by a second, disagreeing
 * source. The open callback is named for what it opens, because the column's
 * own `onOpen` already means "open a tree row".
 */
export interface RoadmapInboxProps extends Omit<InboxScopeBodyProps, 'selectedKey' | 'onOpen'> {
  onOpenProposal: InboxScopeBodyProps['onOpen']
}

export interface RoadmapListColumnProps {
  scope: RoadmapScope
  map: RoadmapMap | undefined
  isLoading: boolean
  isError: boolean
  /** Key of the open row (`keyForOpenItem`), or null. */
  selectedKey: string | null
  onOpen: (row: TreeRow) => void
  onOpenInbox: (productSlug: string) => void
  inbox: RoadmapInboxProps
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
  scope, map, isLoading, isError, selectedKey, onOpen, onOpenInbox, inbox,
}: RoadmapListColumnProps) {
  const products = useMemo(() => map?.products ?? [], [map])
  const entries = useMemo<InitiativeEntry[]>(
    () => products.flatMap((product) => product.initiatives.map((initiative) => ({ product, initiative }))),
    [products],
  )
  const { onOpenProposal, ...inboxRest } = inbox
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="shrink-0 p-2">
        <RoadmapScopeSegment
          scope={scope}
          productCount={products.length}
          initiativeCount={entries.length}
          inboxCount={inbox.proposals.length}
        />
      </div>
      {/* The map's error, not the inbox's — the Inbox scope's body owns its own
          banner, and only that scope's failure is news while you are in it. */}
      {isError && scope !== 'inbox' && (
        <Banner tone="danger" title="Could not load the roadmap." className="mx-2" />
      )}
      {scope === 'map' && (
        <MapScopeBody
          map={map}
          isLoading={isLoading}
          selectedKey={selectedKey}
          onOpen={onOpen}
          onOpenInbox={onOpenInbox}
        />
      )}
      {scope === 'initiatives' && (
        <InitiativesScopeBody
          entries={entries}
          isLoading={isLoading}
          selectedKey={selectedKey}
          onOpen={onOpen}
        />
      )}
      {scope === 'inbox' && (
        <InboxScopeBody {...inboxRest} selectedKey={selectedKey} onOpen={onOpenProposal} />
      )}
    </div>
  )
}

type MapScopeBodyProps = Omit<RoadmapListColumnProps, 'scope' | 'isError' | 'inbox'>

function MapScopeBody({ map, isLoading, selectedKey, onOpen, onOpenInbox }: MapScopeBodyProps) {
  const [tree, setTree] = useState<TreeState>(() => ({ collapsed: loadGroupCollapseNs(COLLAPSE_NS) }))
  const [query, setQuery] = useState('')
  useEffect(() => {
    saveGroupCollapseNs(COLLAPSE_NS, new Set(tree.collapsed))
  }, [tree])
  const rows = useMemo(() => (map ? flattenMap(map, tree, query) : []), [map, tree, query])
  const collapse = useCallback(
    (key: string, collapsed: boolean) => setTree((t) => setCollapsed(t, key, collapsed)),
    [],
  )
  const { containerRef, rovingKey, onKeyDown } = useTreeKeys(rows, selectedKey, collapse)

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
      {/* A real tree: one tab stop, arrows to walk and fold it (`useTreeKeys`).
          The handler hangs off the container, so every row's keys are handled in
          one place and only while focus is inside the tree. */}
      {rows.length > 0 && (
        <div
          ref={containerRef}
          role="tree"
          aria-label={ROADMAP.map}
          onKeyDown={onKeyDown}
          className="flex flex-col gap-1"
        >
          {rows.map((row) => (row.kind === 'initiative' ? (
            <InitiativeCard
              key={row.key}
              rowKey={row.key}
              depth={row.depth}
              initiative={row.initiative}
              selected={selectedKey === row.key}
              focusable={rovingKey === row.key}
              onOpen={() => onOpen(row)}
              expanded={row.expanded}
              hasChildren={row.hasChildren}
              // Same fold state a capability row's chevron and `useTreeKeys`'s
              // ArrowLeft/ArrowRight both toggle through.
              onToggle={() => setTree((t) => toggleKey(t, row.key))}
            />
          ) : (
            <TreeRowCard
              key={row.key}
              row={row}
              selected={selectedKey === row.key}
              focusable={rovingKey === row.key}
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
  // A program row has no stage of its own that a human acts on directly — its
  // phases do — so this scope lists LEAF initiatives only. A phase wears its
  // program as a chip instead (`programTitle`, below) so the context is not lost.
  const leaves = useMemo(() => entries.filter((e) => !e.initiative.hasChildren), [entries])
  // One `programTitleOf` lookup per PRODUCT among the visible leaves, not one
  // per row: a product's `Map` build is the same on every phase that belongs
  // to it, and this scope can list phases from several products at once.
  const programTitleFns = useMemo(() => {
    const fns = new Map<string, (initiative: InitiativeNode) => string | null>()
    for (const e of leaves) {
      if (!fns.has(e.product.slug)) fns.set(e.product.slug, programTitleOf(e.product))
    }
    return fns
  }, [leaves])
  const visible = useMemo(
    () => (q
      ? leaves.filter((e) => `${e.initiative.title ?? ''} ${e.initiative.slug}`.toLowerCase().includes(q))
      : leaves),
    [leaves, q],
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
          // `lookup.ts:programTitleOf` — a titleless program still has to
          // yield a chip, so it falls back to its own slug rather than the
          // lookup's `null`, same as `InitiativePage`'s "Part of" link.
          const programTitle = programTitleFns.get(e.product.slug)?.(e.initiative) ?? null
          return (
            <InitiativeCard
              key={key}
              rowKey={key}
              depth={0}
              rowRole="listitem"
              initiative={e.initiative}
              programTitle={programTitle}
              selected={selectedKey === key}
              onOpen={() => onOpen({
                kind: 'initiative', key, depth: 0, product: e.product, initiative: e.initiative,
                // A flat list, not the tree: there is no fold state here, and
                // `hasChildren` is a fact about the initiative the row already
                // carries — not something this scope renders nested itself.
                expanded: false, hasChildren: e.initiative.hasChildren,
              })}
            />
          )
        }}
      />
    </ScopeBody>
  )
}
