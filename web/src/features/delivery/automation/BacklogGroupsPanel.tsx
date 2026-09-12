import { useState } from 'react'
import { toast } from 'sonner'
import { useBacklogGroups, useDispatchBacklogGroup } from '@/api/hooks'
import type { BacklogGroup } from '@/api/automations'
import { issueUrl } from '@/lib/githubUrl'

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
          <span className="ml-auto rounded bg-accent-wash px-1 text-xs text-accent shrink-0">
            picked up
          </span>
        ) : (
          <button
            type="button"
            onClick={onDispatch}
            disabled={isDispatching}
            className="ml-auto rounded bg-accent px-2 py-0.5 text-xs text-accent-ink hover:bg-accent/90 disabled:opacity-40 shrink-0"
          >
            {isDispatching ? 'Dispatching…' : 'Dispatch'}
          </button>
        )}
      </div>
      {group.rationale && (
        <p className="mt-0.5 text-xs text-ink-muted leading-snug">{group.rationale}</p>
      )}
    </li>
  )
}

/** Planned groups panel with per-group immediate dispatch. */
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
  if (!groups.length) return null

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
    <section className="mt-3 border-t border-edge pt-3">
      <h4 className="text-xs font-semibold uppercase text-ink-muted mb-2">
        Planned groups
        {data?.createdAt && (
          <span className="ml-2 font-normal normal-case">
            planned {data.createdAt.slice(0, 16).replace('T', ' ')}
          </span>
        )}
      </h4>
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
    </section>
  )
}
