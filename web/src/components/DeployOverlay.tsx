// web/src/components/DeployOverlay.tsx
import { Fragment } from 'react'
import { useDeployController } from '@/lib/deployController'
import { useDeployStatus, usePendingDeploy } from '@/api/hooks'
import { DEPLOY_PHASES, phaseState, deployProgress, hostRows } from '@/lib/deployPhases'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'

const DOT: Record<string, string> = {
  done: 'bg-emerald-500', running: 'bg-amber-500 animate-pulse',
  failed: 'bg-red-500', pending: 'bg-gray-300 dark:bg-gray-600',
}

const HOST_DOT: Record<string, string> = {
  ...DOT, offline: 'bg-gray-400', disabled: 'bg-gray-400', unconfigured: 'bg-red-500',
}

export function DeployOverlay() {
  const { visible, intent, dismiss, startError } = useDeployController()
  const { data: deploy, isError, failureCount } = useDeployStatus()
  const { data: pending } = usePendingDeploy()
  if (!visible) return null

  const running = deploy?.running ?? false
  const { done, total } = deployProgress(deploy)
  const pct = Math.round((done / total) * 100)
  const count = pending?.commits.length ?? 0
  // Prefer the controller's actual deploy target: a green-frontier deploy targets an
  // OLDER sha than pending.targetSha (origin/master's red tip), and the overlay must
  // report what is really being shipped. pending.targetSha remains the fallback for
  // the pre-start instant, before the controller has recorded its target.
  const shortSha = (deploy?.targetSha || pending?.targetSha || '').slice(0, 7)
  const reconnecting = (running || intent === 'starting') && isError && failureCount > 0
  // Only highlight the phase that is actively running, never a failed one — a failed phase
  // should show its red dot, not the amber pulse that implies "in progress" (issue #741).
  const activeKey = running
    ? DEPLOY_PHASES.find(p => phaseState(deploy, p.key) === 'running')?.key
    : undefined

  return (
    <Dialog open onClose={() => {}} aria-label="Deploying" dismissable={false} size="md" className="w-[28rem]">
      {intent === 'held-success' ? (
        <div className="text-center py-6">
          <div className="text-3xl">✓</div>
          <p className="mt-2 font-medium text-ink">Deployed{shortSha && <span className="font-mono text-sm text-ink-muted"> · {shortSha}</span>}</p>
          {deploy?.restore?.failed?.length ? (
            <p className="mt-2 text-sm text-amber-600 dark:text-amber-400">
              {deploy.restore.failed.length} session{deploy.restore.failed.length === 1 ? '' : 's'} couldn’t be resumed:{' '}
              <span className="font-mono">{deploy.restore.failed.join(', ')}</span>
            </p>
          ) : null}
        </div>
      ) : intent === 'held-failure' ? (
        <div>
          <h2 className="text-lg font-semibold text-danger">Upgrade failed</h2>
          <p className="mt-2 text-sm text-danger">{startError ?? deploy?.error ?? 'The deploy did not complete.'}</p>
          {deploy?.restore?.failed?.length ? (
            <p className="mt-2 text-sm text-danger">
              {deploy.restore.failed.length} session{deploy.restore.failed.length === 1 ? '' : 's'} failed to resume:{' '}
              <span className="font-mono">{deploy.restore.failed.join(', ')}</span>
            </p>
          ) : null}
          <div className="mt-4 text-right">
            <Button type="button" variant="secondary" size="lg" onClick={dismiss}>Close</Button>
          </div>
        </div>
      ) : (
        <div>
          <h2 className="text-lg font-semibold text-ink">Upgrading ccswitch</h2>
          <p className="text-xs text-ink-muted">
            {count} change{count === 1 ? '' : 's'}{shortSha && <> · <span className="font-mono">{shortSha}</span></>}
          </p>
          <div className="mt-4 h-1.5 w-full rounded bg-gray-200 dark:bg-gray-700 overflow-hidden">
            <div className="h-full bg-amber-500 transition-all duration-500" style={{ width: `${pct}%` }} />
          </div>
          <ol className="mt-4 space-y-2">
            {DEPLOY_PHASES.map(p => {
              const s = phaseState(deploy, p.key)
              const active = p.key === activeKey
              return (
                <Fragment key={p.key}>
                  <li className={`flex items-center gap-3 text-sm ${active ? 'font-medium' : ''}`}>
                    <span className={`h-2.5 w-2.5 rounded-full ${active ? DOT.running : DOT[s]}`} aria-hidden />
                    <span className="w-20 text-ink">{p.label}</span>
                    <span className="flex-1 text-ink-muted">{p.description}</span>
                  </li>
                  {p.key === 'runners' && hostRows(deploy).map(([name, h]) => (
                    <li key={`host:${name}`} className="ml-6 flex items-center gap-3 text-xs">
                      <span className={`h-2 w-2 rounded-full ${HOST_DOT[h.state] ?? DOT.pending}`} aria-hidden />
                      <span className="w-24 font-mono text-ink">{name}</span>
                      <span className="flex-1 text-ink-muted">
                        {h.state === 'running' ? (h.step ?? 'working') : h.state}
                        {h.sha ? <> · <span className="font-mono">{h.sha.slice(0, 7)}</span></> : null}
                        {h.error ? <span className="text-danger"> — {h.error}</span> : null}
                      </span>
                    </li>
                  ))}
                </Fragment>
              )
            })}
          </ol>
          <div className="mt-4 min-h-[1.25rem] text-xs">
            {reconnecting ? (
              <span className="text-amber-600 dark:text-amber-400">Restarting service — reconnecting…</span>
            ) : deploy?.error ? (
              <p className="text-danger">{deploy.error}</p>
            ) : (
              deploy?.warnings?.map((w, i) => (
                <p key={i} className="text-amber-600 dark:text-amber-400">{w}</p>
              ))
            )}
          </div>
        </div>
      )}
    </Dialog>
  )
}
