// The issues under one node of the roadmap — the bottom of the tree, where the
// work actually is. Open first and closed below it, because "what is left" is
// the question the list is opened with; the closed rows stay because they are
// the evidence a capability is finished (finish-line spec §7).
import type { Leaf, LeavesScope } from '@/api/roadmap'
import { useLeaves } from '@/api/hooks'
import { Skeleton } from '@/ui/Skeleton'
import { EmptyState } from '@/ui/EmptyState'
import { Banner } from '@/ui/Banner'
import { Badge } from '@/ui/Badge'
import { ISSUE } from '@/features/products/vocab'
import { tierLabel } from '../roadmapTier'
import { TierDot } from '../rows/TierDot'

function LeafRow({ leaf }: { leaf: Leaf }) {
  const blockers = leaf.blockedBy.length
  return (
    <li className="flex items-center gap-2 rounded-md border border-edge bg-surface p-2 text-sm">
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
      <span className="w-10 shrink-0 text-right text-xs tabular-nums text-ink-muted" title="Priority score">
        {leaf.score?.toFixed(1) ?? '—'}
      </span>
    </li>
  )
}

export function LeafList({ scope }: { scope: LeavesScope }) {
  const { data, isLoading, isError } = useLeaves(scope)
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
  const open = leaves.filter((l) => l.state === 'open')
  const closed = leaves.filter((l) => l.state !== 'open')
  return (
    <div className="flex flex-col gap-4">
      {open.length > 0 && (
        <section>
          <h3 className="mb-1 text-xs font-medium uppercase text-ink-muted">Open · {open.length}</h3>
          <ul className="flex flex-col gap-1">
            {open.map((l) => <LeafRow key={l.id} leaf={l} />)}
          </ul>
        </section>
      )}
      {closed.length > 0 && (
        <section>
          <h3 className="mb-1 text-xs font-medium uppercase text-ink-muted">Closed · {closed.length}</h3>
          <ul className="flex flex-col gap-1">
            {closed.map((l) => <LeafRow key={l.id} leaf={l} />)}
          </ul>
        </section>
      )}
    </div>
  )
}
