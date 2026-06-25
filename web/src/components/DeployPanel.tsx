// web/src/components/DeployPanel.tsx
import { useState } from 'react'
import { usePendingDeploy, useDeployStatus } from '@/api/hooks'
import { useDeployController } from '@/lib/deployController'

export function DeployPanel() {
  const { data: pending } = usePendingDeploy()
  const { data: deploy } = useDeployStatus()
  const { start, starting } = useDeployController()
  const [open, setOpen] = useState(false)

  const behind = pending?.behind ?? false
  const running = deploy?.running ?? false
  const phases = deploy?.phases ?? {}
  const failedResume = deploy?.restore?.failed?.length ?? 0
  const count = pending?.commits.length ?? 0
  const ci = pending?.ci ?? 'unavailable'

  // A prior deploy that failed only at the preflight CI gate is stale once CI is green again.
  const preflightStale = !running && phases.preflight === 'failed' && ci === 'green'
  // Compact, persistent failure fallback for a past (non-running) deploy after the overlay closed.
  const showFailure = !running && !preflightStale
    && (!!deploy?.error || failedResume > 0)

  if (!behind && !showFailure) return null
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
              className="rounded bg-amber-600 px-3 py-1 text-white transition
                         hover:bg-amber-700 active:scale-95
                         focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400
                         disabled:opacity-50 disabled:hover:bg-amber-600 disabled:active:scale-100"
              disabled={!canDeploy || starting}
              onClick={() => start()}
            >{starting ? 'Starting…' : 'Deploy now'}</button>
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
      {showFailure && (
        <div className="mt-2 border-t border-amber-200 pt-2">
          {deploy?.error && <p className="text-xs text-red-600">{deploy.error}</p>}
          {failedResume ? (
            <p className="mt-1 text-xs text-red-600">
              {failedResume} session{failedResume === 1 ? '' : 's'} failed to resume — open them to retry:{' '}
              <span className="font-mono">{deploy?.restore?.failed?.join(', ')}</span>
            </p>
          ) : null}
        </div>
      )}
    </div>
  )
}
