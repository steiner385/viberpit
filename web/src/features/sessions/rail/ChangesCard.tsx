/**
 * Rail card: what the session's worktree has changed against its base — the
 * totals, the first few files, and the way into the Changes tab. Same query
 * (`useSessionDiff` summary + `busy`) as the header chip, so they share a cache
 * entry and the turn-end refetch. A 404 (ended/archived session) is no data.
 */
import { useSessionDiff } from '@/api/hooks'
import { Button } from '@/ui/Button'
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import { cn } from '@/lib/utils'
import { DiffCounts } from '../diff/DiffCounts'
import { RailCard, RailEmpty } from './RailCard'

/** How many files the card lists before deferring to the Changes tab. */
const MAX_FILES = 5

interface Props {
  sessionId: string
  /** A turn is running: its end invalidates the diff (see `useSessionDiff`). */
  busy: boolean
  onOpenChanges: () => void
}

export function ChangesCard({ sessionId, busy, onOpenChanges }: Props) {
  const { data, isError } = useSessionDiff(sessionId, { summary: true, busy })
  const files = data?.files ?? []

  return (
    <RailCard title="Changes">
      {data == null && !isError ? (
        // First fetch still in flight: "No changes yet" would be a claim we can't make.
        <RailEmpty>Loading…</RailEmpty>
      ) : data == null || files.length === 0 ? (
        <RailEmpty>No changes yet</RailEmpty>
      ) : (
        <>
          <p
            data-testid="changes-totals"
            className="text-ink"
            title={data.truncated ? 'Diff truncated — totals are lower bounds' : undefined}
          >
            <DiffCounts additions={data.additions} deletions={data.deletions} />
            <span className="text-ink-muted"> · {files.length} {files.length === 1 ? 'file' : 'files'}</span>
          </p>
          <ul aria-label="Changed files" className="flex min-w-0 flex-col gap-0.5">
            {files.slice(0, MAX_FILES).map((f) => (
              <li key={f.path} className="flex min-w-0 items-center gap-2" title={f.path}>
                <span className="min-w-0 flex-1 truncate font-mono text-ink">{f.path}</span>
                <span className="shrink-0 text-ink-muted">{f.status}</span>
                <DiffCounts additions={f.additions} deletions={f.deletions} className="shrink-0" />
              </li>
            ))}
          </ul>
          <Button
            variant="secondary"
            size="sm"
            onClick={onOpenChanges}
            className={cn('self-start', TOUCH_FLOOR)}
          >
            Open Changes
          </Button>
        </>
      )}
    </RailCard>
  )
}
