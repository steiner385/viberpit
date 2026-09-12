// The issues under one node of the roadmap — the bottom of the tree, where the
// work actually is. Open first and closed below it, because "what is left" is
// the question the list is opened with; the closed rows stay because they are
// the evidence a capability is finished (finish-line spec §7).
import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { Leaf, LeavesScope, ProductNode, CapabilityNode } from '@/api/roadmap'
import { useLeaves, useRemoveDependency } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Skeleton } from '@/ui/Skeleton'
import { EmptyState } from '@/ui/EmptyState'
import { Banner } from '@/ui/Banner'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { ISSUE, bouncedLabel, reasonLabel } from '@/features/products/vocab'
import { SESSIONS_PATH } from '@/session/sessionNavState'
import { issueUrl } from '@/lib/githubUrl'
import { tierLabel } from '../roadmapTier'
import { TierDot } from '../rows/TierDot'
import { flattenCapabilities } from '../lookup'
import { LeafScoreButton, LeafCapabilityChip } from './LeafEditors'
import { leafRows, type LeafListRow } from './leafRows'
import { leafNeedsAttention } from './leafAttention'

/** `owner/repo#N` → `#N` when it is the leaf's own repo; the full ref otherwise. */
function shortRef(ref: string, ownRepo: string): string {
  const [repo, num] = ref.split('#')
  return repo === ownRepo ? `#${num}` : ref
}

function BlockerRow({ leaf, blockerRef }: { leaf: Leaf; blockerRef: string }) {
  const remove = useRemoveDependency()
  const [repo, num] = blockerRef.split('#')
  return (
    <li className="flex flex-col gap-1">
      <span className="flex items-center gap-2">
        <a
          href={issueUrl(repo ?? '', Number(num))}
          target="_blank"
          rel="noreferrer"
          className="text-ink hover:underline"
        >
          {shortRef(blockerRef, leaf.repo)}
        </a>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label={`Remove blocker ${blockerRef}`}
          loading={remove.isPending}
          onClick={() => remove.mutate({ workItemId: leaf.id, blocker: blockerRef })}
        >
          Remove
        </Button>
      </span>
      {remove.isError && (
        <Banner tone="danger" title={extractMutationErrorMessage(remove.error, 'Could not remove the dependency')} />
      )}
    </li>
  )
}

function LeafRow({ leaf, capabilities }: { leaf: Leaf; capabilities: { node: CapabilityNode; depth: number }[] }) {
  const blockers = leaf.blockedBy.length
  const needsAttention = leafNeedsAttention(leaf)
  return (
    <div className="flex flex-col gap-1 rounded-md border border-edge bg-surface p-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <TierDot tier={leaf.tier} />
        {/* GitHub owns the issue; the row links out rather than pretending to a
            detail page ViberPit does not have. A mirrored row can be missing its
            URL, and a dead anchor is worse than plain text — it is focusable,
            announced as a link and goes nowhere. */}
        {leaf.url
          ? (
            <a
              href={leaf.url}
              target="_blank"
              rel="noreferrer"
              className="min-w-0 flex-1 basis-48 truncate text-ink hover:underline"
            >
              #{leaf.number} {leaf.title ?? leaf.ref}
            </a>
          )
          : <span className="min-w-0 flex-1 basis-48 truncate text-ink">#{leaf.number} {leaf.title ?? leaf.ref}</span>}
        {/* The dot is decorative, so the tier is said in words for a screen
            reader — the same words the pills elsewhere use. */}
        <span className="sr-only">{tierLabel(leaf.tier)}</span>
        {/* `data-testid` so the row's test can assert the chip is ABSENT as an
            element: a text query cannot tell "no chip" from an empty pill. */}
        {leaf.state === 'open' && leaf.reason && (
          <Badge tone="warning" data-testid="leaf-reason">
            {leaf.reason === 'needs_spec' ? bouncedLabel(leaf.bounces) : reasonLabel(leaf.reason)}
          </Badge>
        )}
        {leaf.sessionStatus && <Badge tone="success">session</Badge>}
        {blockers > 0 && (
          <Badge tone="danger" count={blockers} aria-label={`${blockers} blocker${blockers === 1 ? '' : 's'}`} />
        )}
        <LeafCapabilityChip leaf={leaf} capabilities={capabilities} />
        <LeafScoreButton leaf={leaf} />
      </div>
      {needsAttention && (
        <div className="flex flex-col gap-1 pl-6 text-xs text-ink-muted">
          <p>
            {leaf.reason === 'needs_spec'
              ? 'Repeated attempts have not merged. Update the issue description with clearer requirements and acceptance criteria before another attempt.'
              : leaf.tier === 'blocked'
                ? blockers > 0
                  ? 'Resolve the blocking issues below, or remove a dependency if it no longer applies.'
                  : 'This issue is marked blocked. Review its discussion and status on GitHub to identify what must change.'
                : 'A decision or input is needed. Review the issue discussion for the request; if a session is waiting, respond in Sessions.'}
          </p>
          {leaf.tier === 'needs-you' && leaf.sessionStatus && leaf.reason !== 'needs_spec' && (
            <a href={SESSIONS_PATH} className="text-accent hover:underline">View sessions</a>
          )}
          {leaf.url
            ? <a href={leaf.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                {leaf.reason === 'needs_spec' ? 'Edit issue description on GitHub' : 'Review issue on GitHub'}
              </a>
            : <span>Issue link unavailable; find {leaf.ref} in its repository.</span>}
        </div>
      )}
      {leaf.state === 'open' && blockers > 0 && (
        // GitHub owns the edge, but removing it is the one disposition a blocked
        // leaf has, so the row offers it instead of pointing at a blocker count.
        <ul aria-label="Blocked by" className="flex flex-wrap gap-x-4 gap-y-1 pl-6 text-xs text-ink-muted">
          {leaf.blockedBy.map((blockerRef) => <BlockerRow key={blockerRef} leaf={leaf} blockerRef={blockerRef} />)}
        </ul>
      )}
    </div>
  )
}

export function LeafList({ scope, product, attentionOnly = false }: { scope: LeavesScope; product: ProductNode | null; attentionOnly?: boolean }) {
  const { data, isLoading, isError } = useLeaves(scope)
  const capabilities = useMemo(() => (product ? flattenCapabilities(product.capabilities) : []), [product])
  // The leaves are their own query, so a failure here costs only this panel;
  // everything around it came from the map and stays on screen.
  if (isError && !data) {
    return (
      <Banner tone="danger" title={`Could not load the ${ISSUE.plural.toLowerCase()}.`}>
        The list refreshes on its own, so it will fill in once the connection is back.
      </Banner>
    )
  }
  if (isLoading && !data) return <Skeleton lines={5} />
  const leaves = attentionOnly ? (data ?? []).filter(leafNeedsAttention) : (data ?? [])
  if (leaves.length === 0) return <EmptyState title={attentionOnly ? 'No issues need your attention' : `No ${ISSUE.plural.toLowerCase()} here yet`} />
  return <VirtualRows rows={leafRows(leaves)} capabilities={capabilities} />
}

// jsdom reports a zero-height scroll container, so the window collapses to the
// first row plus `overscan`; the tests rely on that (short fixtures render
// whole, long ones do not).
const OVERSCAN = 10

function VirtualRows({ rows, capabilities }: { rows: LeafListRow[]; capabilities: { node: CapabilityNode; depth: number }[] }) {
  const listRef = useRef<HTMLDivElement>(null)
  // How far the list starts down the SCROLL CONTAINER's content — the offset the
  // virtualizer subtracts from the page's scrollTop. It has to be measured:
  // `offsetTop` is relative to the nearest positioned ancestor, and nothing
  // between this list and <body> is positioned, so it would also count the top
  // bar and the PageHeader, which are outside the scroll container. Making
  // `PageContent` positioned instead would turn it into the containing block for
  // every absolutely positioned descendant of a page, and clip them.
  const [scrollMargin, setScrollMargin] = useState(0)
  useLayoutEffect(() => {
    const list = listRef.current
    const scrollEl = list?.closest<HTMLElement>('[data-page-scroll]')
    if (!list || !scrollEl) return
    const measure = () => setScrollMargin(
      scrollEl.scrollTop + list.getBoundingClientRect().top - scrollEl.getBoundingClientRect().top,
    )
    measure()
    // The tiles and tabs above the list resize (a banner arrives, text wraps),
    // which moves where the list starts without re-rendering it.
    const ro = new ResizeObserver(measure)
    ro.observe(scrollEl)
    return () => ro.disconnect()
  }, [rows.length])
  // The page scrolls (`PageContent`), not this list — it is one panel among the
  // tiles and tabs above it — so the virtualizer measures against the page's
  // scroll element and offsets by where the list starts inside it.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => listRef.current?.closest<HTMLElement>('[data-page-scroll]') ?? null,
    estimateSize: (i) => (rows[i].kind === 'header' ? 28 : 44),
    overscan: OVERSCAN,
    getItemKey: (i) => rows[i].key,
    scrollMargin,
  })
  return (
    <div ref={listRef} role="list" aria-label={ISSUE.plural} className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
      {virtualizer.getVirtualItems().map((item) => {
        const row = rows[item.index]
        return (
          <div
            key={item.key}
            role="listitem"
            // Only a window of the list is in the DOM, so the row says where it
            // sits in the whole of it — otherwise a screen reader announces the
            // handful of mounted rows as the entire list.
            aria-setsize={rows.length}
            aria-posinset={item.index + 1}
            data-index={item.index}
            ref={virtualizer.measureElement}
            className="absolute left-0 top-0 w-full pb-1"
            style={{ transform: `translateY(${item.start - virtualizer.options.scrollMargin}px)` }}
          >
            {row.kind === 'header'
              ? <h3 className="mb-1 text-xs font-medium uppercase text-ink-muted">{row.label}</h3>
              : <LeafRow leaf={row.leaf} capabilities={capabilities} />}
          </div>
        )
      })}
    </div>
  )
}
