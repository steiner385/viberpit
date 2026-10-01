import { useState } from 'react'
import { toast } from 'sonner'
import { Check } from 'lucide-react'
import { useBacklogGroups, useDispatchBacklogGroup } from '@/api/hooks'
import type { BacklogGroup } from '@/api/automations'
import { Button } from '@/ui/Button'
import { StatusPill } from '@/ui/StatusPill'
import { issueUrl } from '@/lib/githubUrl'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'

function GroupRow({
  group,
  onDispatch,
  isDispatching,
}: {
  group: BacklogGroup
  onDispatch(): void
  isDispatching: boolean
}) {
  return (
    <li className="rounded border border-edge p-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        {group.repo && (
          <span className="text-xs text-ink-muted shrink-0">{group.repo}</span>
        )}
        {group.refs.map((ref) => {
          const [repo, num] = ref.split('#')
          return (
            <a
              key={ref}
              href={issueUrl(repo ?? '', Number(num))}
              target="_blank"
              rel="noreferrer"
              title={group.titles[ref]}
              className="hover:underline"
            >
              #{num}
              {group.titles[ref] ? ` ${group.titles[ref]}` : ''}
            </a>
          )
        })}
        {group.consumedAt ? (
          // Tone plus word (DESIGN.md): the system's status pill, not a
          // hand-rolled wash span.
          <StatusPill label="Picked up" Icon={Check} tone="success" className="ml-auto shrink-0" />
        ) : (
          // Dispatch spawns a session — the heaviest action on the tab — so it
          // is the `lg` primary button (44px, the size every primary confirm
          // takes), not a 65×20 hand-rolled wash beside a 40px link
          // (2026-10-01 Delivery critique, P1).
          <Button
            type="button"
            size="lg"
            variant="primary"
            className="ml-auto shrink-0"
            loading={isDispatching}
            onClick={onDispatch}
          >
            {isDispatching ? 'Dispatching…' : 'Dispatch'}
          </Button>
        )}
      </div>
      {group.rationale && (
        <p className="mt-0.5 text-xs text-ink-muted leading-snug">{group.rationale}</p>
      )}
    </li>
  )
}

/** The planned groups with per-group immediate dispatch. The heading row
 *  ("Planned groups" + Custom dispatch) is the caller's, so an empty plan is
 *  still a sentence under it rather than a heading over nothing. */
export function BacklogGroupsPanel({
  enabled,
  onOpenSession,
}: {
  enabled: boolean
  onOpenSession?: (id: string) => void
}) {
  const { data } = useBacklogGroups(enabled)
  const dispatch = useDispatchBacklogGroup()
  const [dispatchingId, setDispatchingId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const groups = data?.groups ?? []
  if (!data) return null
  if (!groups.length) return <p className="text-sm text-ink-muted">No planned groups yet.</p>

  async function handleDispatch(group: BacklogGroup) {
    setDispatchingId(group.id)
    setError(null)
    try {
      const r = await dispatch.mutateAsync(group.refs)
      if (r.status === 'spawned') {
        // The new session may open on another page, so success has no
        // reliable inline owner here — keep the toast for it.
        toast.success(r.session_name ? `Session spawned: ${r.session_name}` : 'Session spawned')
        if (r.session_id) onOpenSession?.(r.session_id)
      } else {
        setError(`Dispatch failed: ${r.status}`)
      }
    } catch {
      setError('Failed to dispatch group')
    } finally {
      setDispatchingId(null)
    }
  }

  return (
    <div className="space-y-2">
      {isValidTimestamp(data.createdAt) && (
        <p className="text-xs text-ink-muted">
          <time dateTime={data.createdAt} title={data.createdAt}>Planned {relTime(data.createdAt)}</time>
        </p>
      )}
      {data.degraded && (
        <p role="status" className="text-xs text-warning mb-2 leading-snug">
          The grouping model&apos;s reply could not be parsed, so this plan is every
          issue on its own. It retries at the next refresh.
        </p>
      )}
      {error && <p role="alert" className="text-sm text-danger mb-2">{error}</p>}
      <ul className="space-y-2">
        {groups.map((g) => (
          <GroupRow
            key={g.id}
            group={g}
            onDispatch={() => handleDispatch(g)}
            isDispatching={dispatchingId === g.id}
          />
        ))}
      </ul>
    </div>
  )
}
