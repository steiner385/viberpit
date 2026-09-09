// The ranked queue — the same order the runner picks work from (spec 003 C1),
// so a human can see what the machine will do next and disagree before it does.
import { useQueue } from '@/api/hooks'
import { EmptyState } from '@/ui/EmptyState'
import { Skeleton } from '@/ui/Skeleton'

export function RankedList({ productSlug }: { productSlug?: string }) {
  const { data, isLoading } = useQueue()
  const entries = (data?.entries ?? []).filter((e) => !productSlug || e.product === productSlug)

  if (isLoading && !data) return <Skeleton lines={5} />
  if (entries.length === 0) {
    return (
      <EmptyState
        title="Nothing ranked yet"
        description="Scores come from triage. Accept a few proposals and the queue fills in."
      />
    )
  }
  return (
    // The server returns the queue already ordered; an `ol` says so, and the
    // rendered rank is the position in it.
    <ol className="flex flex-col gap-1" aria-label="Ranked queue">
      {entries.map((e, i) => (
        <li key={e.ref} className="flex items-baseline gap-3 rounded-md border border-edge bg-surface p-3 text-sm">
          <span className="w-6 shrink-0 text-right tabular-nums text-ink-faint">{i + 1}</span>
          <span className="w-10 shrink-0 tabular-nums text-ink-muted">{e.score?.toFixed(1) ?? '—'}</span>
          <span className="min-w-0 flex-1 truncate">
            {/* An entry with no URL is still real work; it just has nowhere to
                go, so it must not render as a link that does nothing. */}
            {e.url
              ? <a href={e.url} target="_blank" rel="noreferrer" className="text-ink hover:underline">#{e.number} {e.title ?? e.ref}</a>
              : <span className="text-ink">#{e.number} {e.title ?? e.ref}</span>}
            <span className="ml-2 text-xs text-ink-muted">{e.repo}</span>
          </span>
        </li>
      ))}
    </ol>
  )
}
