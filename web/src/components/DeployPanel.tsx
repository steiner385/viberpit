// web/src/components/DeployPanel.tsx
import { useState } from 'react'
import { usePendingDeploy, useDeployStatus, useStartDeploy } from '@/api/hooks'

const PHASES = ['preflight', 'save', 'update', 'report', 'restart', 'resume'] as const
const STATE_ICON: Record<string, string> = {
  pending: '○', running: '◐', done: '●', failed: '✗',
}

export function DeployPanel() {
  const { data: pending } = usePendingDeploy()
  const { data: deploy } = useDeployStatus()
  const start = useStartDeploy()
  const [open, setOpen] = useState(false)

  const behind = pending?.behind ?? false
  const running = deploy?.running ?? false
  const hasPhases = deploy && Object.keys(deploy.phases ?? {}).length > 0
  if (!behind && !running && !hasPhases) return null

  const count = pending?.commits.length ?? 0
  const ci = pending?.ci ?? 'unavailable'
  const canDeploy = behind && !running && ci === 'green'
  const reason = running ? 'deploying…' : ci !== 'green' ? `CI ${ci}` : ''

  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950/30 p-3 text-sm">
      {behind && (
        <div className="flex items-center justify-between gap-3">
          <button className="font-medium" onClick={() => setOpen(o => !o)}>
            {count} change{count === 1 ? '' : 's'} pending deploy {open ? '▾' : '▸'}
          </button>
          <div className="flex items-center gap-2">
            {reason && <span className="text-xs text-amber-700 dark:text-amber-400">{reason}</span>}
            <button
              className="rounded bg-amber-600 px-3 py-1 text-white disabled:opacity-50"
              disabled={!canDeploy}
              onClick={() => start.mutate()}
            >Deploy now</button>
          </div>
        </div>
      )}
      {open && pending && (
        <ul className="mt-2 space-y-1">
          {pending.commits.map(c => (
            <li key={c.sha} className="flex gap-2 font-mono text-xs">
              <span className="text-amber-700 dark:text-amber-400">{c.sha.slice(0, 7)}</span>
              <span className="flex-1 truncate">{c.subject}</span>
              <span className="text-gray-500">{c.author}</span>
            </li>
          ))}
        </ul>
      )}
      {(running || hasPhases) && deploy && (
        <div className="mt-2 border-t border-amber-200 pt-2">
          <ol className="space-y-0.5">
            {PHASES.map(p => {
              const s = deploy.phases?.[p] ?? 'pending'
              return (
                <li key={p} className="flex gap-2 text-xs">
                  <span aria-hidden>{STATE_ICON[s] ?? '○'}</span>
                  <span className={s === 'failed' ? 'text-red-600' : ''}>{p}</span>
                  <span className="text-gray-500">{s}</span>
                </li>
              )
            })}
          </ol>
          {deploy.error && <p className="mt-1 text-xs text-red-600">{deploy.error}</p>}
          {deploy.warnings?.map((w, i) => (
            <p key={i} className="mt-1 text-xs text-amber-700">{w}</p>
          ))}
          {deploy.restore?.failed?.length ? (
            <p className="mt-1 text-xs text-red-600">
              {deploy.restore.failed.length} session{deploy.restore.failed.length === 1 ? '' : 's'} failed to resume — open them to retry:{' '}
              <span className="font-mono">{deploy.restore.failed.join(', ')}</span>
            </p>
          ) : null}
        </div>
      )}
    </div>
  )
}
