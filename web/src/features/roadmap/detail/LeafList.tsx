// The issues under one node of the roadmap — the bottom of the tree, where the
// work actually is. Open first and closed below it, because "what is left" is
// the question the list is opened with; the closed rows stay because they are
// the evidence a capability is finished (finish-line spec §7).
import { useContext, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { Leaf, LeavesScope, ProductNode, CapabilityNode } from '@/api/roadmap'
import { useLeaves, useRemoveDependency } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Skeleton } from '@/ui/Skeleton'
import { EmptyState } from '@/ui/EmptyState'
import { Banner } from '@/ui/Banner'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { SectionHeading } from '@/ui/SectionHeading'
import { ISSUE, bouncedLabel, reasonLabel, holdLabel } from '@/features/products/vocab'
import { SESSIONS_PATH } from '@/session/sessionNavState'
import { issueUrl } from '@/lib/githubUrl'
import { tierLabel } from '../roadmapTier'
import { TierDot } from '../rows/TierDot'
import { flattenCapabilities } from '../lookup'
import { LeafScoreButton, LeafCapabilityChip } from './LeafEditors'
import { leafRows, type LeafListRow } from './leafRows'
import { leafNeedsAttention } from './leafAttention'
import { IssueDispositionContext } from './IssueDispositionContext'

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

const NEEDS_SPEC_HINT = 'Repeated attempts have not merged. Update the issue description with clearer requirements and acceptance criteria before another attempt.'

/** The owner-action hold's own sentence, or null when the row is not held that
 *  way. It is APPENDED to the needs-spec instruction rather than replacing it:
 *  the runner's give-up writes the hold while the bounce rule sets `needs_spec`,
 *  so a row commonly has both — and editing the body is itself a release signal
 *  (spec 2026-09-18 §7), so dropping "edit the body first" would hide the fix. */
function holdSentence(leaf: Leaf): string | null {
  if (leaf.holdKind !== 'owner_action') return null
  return `${holdLabel('owner_action')}${leaf.holdReason ? `: ${leaf.holdReason}` : ''}.`
}

function LeafRow({ leaf, capabilities, onReview }: { leaf: Leaf; capabilities: { node: CapabilityNode; depth: number }[]; onReview: (id: string) => void }) {
  const blockers = leaf.blockedBy.length
  const needsAttention = leafNeedsAttention(leaf)
  const held = holdSentence(leaf)
  return (
    <div className="flex flex-col gap-1 rounded-md border border-edge bg-surface p-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <TierDot tier={leaf.tier} />
        {leaf.kind === 'issue' ? <button
          type="button"
          className="min-w-0 flex-1 basis-48 truncate text-left text-ink hover:underline focus-visible:outline-accent"
          onClick={() => onReview(leaf.id)}
        >
          #{leaf.number} {leaf.title ?? leaf.ref}
        </button> : leaf.url ? (
          <a href={leaf.url} target="_blank" rel="noreferrer" className="min-w-0 flex-1 basis-48 truncate text-ink hover:underline">#{leaf.number} {leaf.title ?? leaf.ref}</a>
        ) : <span className="min-w-0 flex-1 basis-48 truncate text-ink">#{leaf.number} {leaf.title ?? leaf.ref}</span>}
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
        {leaf.state === 'open' && leaf.holdKind && (
          <Badge tone={leaf.holdKind === 'owner_action' || leaf.holdKind === 'decision' ? 'warning' : 'info'} data-testid="leaf-hold">
            {holdLabel(leaf.holdKind)}
          </Badge>
        )}
        {leaf.sessionStatus && <Badge tone="success">session</Badge>}
        {blockers > 0 && (
          <Badge tone="danger" count={blockers} aria-label={`${blockers} blocker${blockers === 1 ? '' : 's'}`} />
        )}
        <LeafCapabilityChip leaf={leaf} capabilities={capabilities} />
        <LeafScoreButton leaf={leaf} />
      </div>
      {needsAttention && leaf.kind === 'issue' && (
        <div className="flex flex-col gap-1 pl-6 text-xs text-ink-muted">
          <p>
            {leaf.reason === 'needs_spec'
              ? `${NEEDS_SPEC_HINT}${held ? ` ${held}` : ''}`
              : held
              ? `${held} Retry to return it to the queue, or open the issue to decide.`
              : leaf.tier === 'blocked'
                ? blockers > 0
                  ? 'Resolve the blocking issues below, or remove a dependency if it no longer applies.'
                  : 'This issue is marked blocked. Review its details, clarify what must change, or close it if no work remains.'
                : 'Read the issue, update its details, or close it as resolved or won’t do. If a session needs input, respond in Sessions.'}
          </p>
          {leaf.tier === 'needs-you' && leaf.sessionStatus && leaf.reason !== 'needs_spec' && (
            <a href={SESSIONS_PATH} className="text-accent hover:underline">View sessions</a>
          )}

        </div>
      )}
      <div className="flex flex-wrap items-center gap-3 pl-6">
        {leaf.kind === 'issue' && <Button variant="secondary" size="sm" onClick={() => onReview(leaf.id)}>{leaf.state === 'open' ? 'Review and decide' : 'View issue'}</Button>}
        {leaf.url && <a href={leaf.url} target="_blank" rel="noreferrer" className="text-xs text-accent hover:underline">View on GitHub</a>}
      </div>
      {leaf.state === 'open' && blockers > 0 && (
        // Removing a dependency remains available alongside issue decisions.
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
  const reviewIssue = useContext(IssueDispositionContext)
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
              ? <SectionHeading className="mb-1">{row.label}</SectionHeading>
              : <LeafRow leaf={row.leaf} capabilities={capabilities} onReview={(id) => reviewIssue?.(id)} />}
          </div>
        )
      })}
    </div>
  )
}
