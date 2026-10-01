// web/src/shell/DeployOverlay.tsx
import { Fragment } from 'react'
import { BRAND_NAME } from '@/brand'
import { useDeployController } from '@/lib/deployController'
import { useDeployStatus, usePendingDeploy } from '@/api/hooks'
import { DEPLOY_PHASES, phaseState, deployProgress, hostRows } from '@/lib/deployPhases'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'

const DOT: Record<string, string> = {
  done: 'bg-success', running: 'bg-warning motion-safe:animate-pulse',
  failed: 'bg-danger', pending: 'bg-edge-strong',
}

const HOST_DOT: Record<string, string> = {
  ...DOT, offline: 'bg-neutral', disabled: 'bg-neutral', unconfigured: 'bg-danger',
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

  // The dialog is named by its state, so a screen reader hears which of the
  // three it is: a success that auto-dismissed after 2 s under a dialog still
  // called "Deploying" was never announced at all (2026-10-01 shell critique,
  // P2). "Deploy", never "Upgrade": the banner, Settings › Deploy and the nav
  // dot all use the one verb.
  const label = intent === 'held-success' ? 'Deployed' : intent === 'held-failure' ? 'Deploy failed' : 'Deploying'

  return (
    <Dialog open onClose={() => {}} aria-label={label} dismissable={false} size="md">
      {intent === 'held-success' ? (
        // Held until closed, and announced: the end of a deploy is the moment
        // the operator wants confirmed, not a ✓ that is gone before the eye
        // lands. Failure already held; success now does the same.
        <div>
          <div role="status" className="text-center py-6">
            <div className="text-3xl text-success" aria-hidden="true">✓</div>
            <h2 className="mt-2 text-lg font-semibold text-ink">Deployed{shortSha && <span className="font-mono text-sm text-ink-muted"> · {shortSha}</span>}</h2>
            {deploy?.restore?.failed?.length ? (
              <p className="mt-2 text-sm text-warning">
                {deploy.restore.failed.length} session{deploy.restore.failed.length === 1 ? '' : 's'} couldn’t be resumed:{' '}
                <span className="font-mono">{deploy.restore.failed.join(', ')}</span>
              </p>
            ) : null}
          </div>
          <div className="text-right">
            <Button type="button" variant="secondary" size="lg" onClick={dismiss}>Close</Button>
          </div>
        </div>
      ) : intent === 'held-failure' ? (
        <div>
          <h2 className="text-lg font-semibold text-danger">Deploy failed</h2>
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
          <h2 className="text-lg font-semibold text-ink">Deploying {BRAND_NAME}</h2>
          <p className="text-xs text-ink-muted">
            {count} change{count === 1 ? '' : 's'}{shortSha && <> · <span className="font-mono">{shortSha}</span></>}
          </p>
          <div className="mt-4 h-1.5 w-full rounded bg-surface-raised overflow-hidden">
            <div className="h-full bg-warning-strong transition-all duration-500" style={{ width: `${pct}%` }} />
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
              <span className="text-warning">Restarting service — reconnecting…</span>
            ) : deploy?.error ? (
              <p className="text-danger">{deploy.error}</p>
            ) : (
              deploy?.warnings?.map((w, i) => (
                <p key={i} className="text-warning">{w}</p>
              ))
            )}
          </div>
        </div>
      )}
    </Dialog>
  )
}
