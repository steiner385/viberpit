// The issues under one node of the roadmap — the bottom of the tree, where the
// work actually is. Open first and closed below it, because "what is left" is
// the question the list is opened with; the closed rows stay because they are
// the evidence a capability is finished (finish-line spec §7).
import { useMemo, useRef } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { Leaf, LeavesScope, ProductNode, CapabilityNode } from '@/api/roadmap'
import { useLeaves, useRemoveDependency } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Skeleton } from '@/ui/Skeleton'
import { EmptyState } from '@/ui/EmptyState'
import { Banner } from '@/ui/Banner'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { ISSUE } from '@/features/products/vocab'
import { issueUrl } from '@/lib/githubUrl'
import { tierLabel } from '../roadmapTier'
import { TierDot } from '../rows/TierDot'
import { flattenCapabilities } from '../lookup'
import { LeafScoreButton, LeafCapabilityChip } from './LeafEditors'
import { leafRows, type LeafListRow } from './leafRows'

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
  return (
    <div className="flex flex-col gap-1 rounded-md border border-edge bg-surface p-2 text-sm">
      <div className="flex items-center gap-2">
        <TierDot tier={leaf.tier} />
        {/* GitHub owns the issue; the row links out rather than pretending to a
            detail page ccswitch does not have. A mirrored row can be missing its
            URL, and a dead anchor is worse than plain text — it is focusable,
            announced as a link and goes nowhere. */}
        {leaf.url
          ? (
            <a
              href={leaf.url}
              target="_blank"
              rel="noreferrer"
              className="min-w-0 flex-1 truncate text-ink hover:underline"
            >
              #{leaf.number} {leaf.title ?? leaf.ref}
            </a>
          )
          : <span className="min-w-0 flex-1 truncate text-ink">#{leaf.number} {leaf.title ?? leaf.ref}</span>}
        {/* The dot is decorative, so the tier is said in words for a screen
            reader — the same words the pills elsewhere use. */}
        <span className="sr-only">{tierLabel(leaf.tier)}</span>
        {leaf.sessionStatus && <Badge tone="success">session</Badge>}
        {blockers > 0 && (
          <Badge tone="danger" count={blockers} aria-label={`${blockers} blocker${blockers === 1 ? '' : 's'}`} />
        )}
        <LeafCapabilityChip leaf={leaf} capabilities={capabilities} />
        <LeafScoreButton leaf={leaf} />
      </div>
      {blockers > 0 && (
        // GitHub owns the edge, but removing it is the one disposition a blocked
        // leaf has, so the row offers it instead of pointing at a blocker count.
        <ul aria-label="Blocked by" className="flex flex-wrap gap-x-4 gap-y-1 pl-6 text-xs text-ink-muted">
          {leaf.blockedBy.map((blockerRef) => <BlockerRow key={blockerRef} leaf={leaf} blockerRef={blockerRef} />)}
        </ul>
      )}
    </div>
  )
}

export function LeafList({ scope, product }: { scope: LeavesScope; product: ProductNode | null }) {
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
  const leaves = data ?? []
  if (leaves.length === 0) return <EmptyState title={`No ${ISSUE.plural.toLowerCase()} here yet`} />
  return <VirtualRows rows={leafRows(leaves)} capabilities={capabilities} />
}

// jsdom reports a zero-height scroll container, so the window collapses to the
// first row plus `overscan`; the tests rely on that (short fixtures render
// whole, long ones do not).
const OVERSCAN = 10

function VirtualRows({ rows, capabilities }: { rows: LeafListRow[]; capabilities: { node: CapabilityNode; depth: number }[] }) {
  const listRef = useRef<HTMLDivElement>(null)
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
    scrollMargin: listRef.current?.offsetTop ?? 0,
  })
  return (
    <div ref={listRef} role="list" aria-label={ISSUE.plural} className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
      {virtualizer.getVirtualItems().map((item) => {
        const row = rows[item.index]
        return (
          <div
            key={item.key}
            role="listitem"
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
