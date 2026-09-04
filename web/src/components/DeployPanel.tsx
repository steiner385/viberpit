// web/src/components/DeployPanel.tsx
import { useState } from 'react'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { usePendingDeploy, useDeployStatus } from '@/api/hooks'
import { useDeployController } from '@/lib/deployController'

// The failure notice is dismissed by CONTENT, not as a boolean: `gaveUp` is the daemon's
// live set and only changes when a human retries/archives, so a bare "hidden" flag would
// either mask the next failure or (if session-scoped) reappear on every reload — the
// original complaint. Persisting the signature of what was dismissed means the same
// failure stays gone across reloads while any new session id or error text brings the
// notice back on its own. Guarded like `lib/lastRepo.ts`: a blocked localStorage degrades
// to "not dismissed", never a crash.
const DISMISSED_KEY = 'ccswitch.deployFailureDismissed'

function failureSignature(error: string | null, failed: string[], gaveUp: string[]): string {
  return JSON.stringify({ error, failed: [...failed].sort(), gaveUp: [...gaveUp].sort() })
}

function readDismissedSignature(): string | null {
  try {
    return window.localStorage.getItem(DISMISSED_KEY)
  } catch {
    return null
  }
}

function writeDismissedSignature(sig: string): void {
  try {
    window.localStorage.setItem(DISMISSED_KEY, sig)
  } catch {
    /* ignore — persistence is best-effort */
  }
}

export function DeployPanel() {
  const { data: pending } = usePendingDeploy()
  const { data: deploy } = useDeployStatus()
  const { start, starting } = useDeployController()
  const [open, setOpen] = useState(false)

  const behind = pending?.behind ?? false
  const running = deploy?.running ?? false
  const phases = deploy?.phases ?? {}
  // `failed` = still being auto-recovered by autopilot; `gaveUp` = recovery capped
  // out (needs a human). Both are the daemon's LIVE set — the banner clears itself as
  // sessions come back, so no manual "open to retry" is needed for the transient case.
  const failed = deploy?.restore?.failed ?? []
  const gaveUp = deploy?.restore?.gaveUp ?? []
  const unresolved = failed.length + gaveUp.length
  const count = pending?.commits.length ?? 0
  const ci = pending?.ci ?? 'unavailable'
  // Green frontier: the tip is red/pending but an older pending commit is green — the
  // Deploy button targets that sha and the banner says honestly how much it covers.
  const tipCi = pending?.tipCi ?? ci
  const deployableSha = pending?.deployableSha ?? ''
  const deployableCount = pending?.deployableCount ?? 0
  const partial = ci === 'green' && tipCi !== 'green' && !!deployableSha && deployableCount > 0

  // A prior deploy that failed only at the preflight CI gate is stale once CI is green again.
  const preflightStale = !running && phases.preflight === 'failed' && ci === 'green'
  // A past deploy error is actionable only while commits still await deploy. A restart/health-gate
  // failure rolls back to the previous version, so once the checkout is caught up or ahead of origin
  // (behind=false) the error is pure history — hide it. Session-resume failures are independent (a
  // successful daemon deploy leaves behind=false yet may have lost sessions) and always surface.
  const showError = !running && !preflightStale && behind && !!deploy?.error
  const hasFailure = showError || (!running && unresolved > 0)
  // Only the failure notice is dismissible. Pending commits are actionable (Deploy button)
  // and clear on their own once deployed — hiding them would hide the only way to deploy.
  const [dismissedSig, setDismissedSig] = useState(readDismissedSignature)
  const failureSig = failureSignature(showError ? deploy?.error ?? null : null, failed, gaveUp)
  const showFailure = hasFailure && dismissedSig !== failureSig
  const dismissFailure = () => {
    writeDismissedSignature(failureSig)
    setDismissedSig(failureSig)
  }

  if (!behind && !showFailure) return null
  const canDeploy = behind && !running && ci === 'green'
  const reason = running ? 'deploying…'
    : partial ? `${deployableCount} of ${count} deployable (tip CI ${tipCi})`
    : ci !== 'green' ? `CI ${ci}` : ''

  return (
    <Banner
      tone="warning"
      // Not a live region: commits, phase state and error text all change while a deploy
      // runs, and role="status" would announce every one of those to a screen reader.
      role="none"
      title={behind ? (
        <button type="button" onClick={() => setOpen((o) => !o)}>
          {count} change{count === 1 ? '' : 's'} pending deploy {open ? '▾' : '▸'}
        </button>
      ) : undefined}
      actions={behind ? (
        <>
          {reason && <span className="text-xs text-warning">{reason}</span>}
          <Button
            size="sm"
            variant="secondary"
            disabled={!canDeploy || starting}
            onClick={() => start(partial ? deployableSha : undefined)}
          >
            {starting ? 'Starting…' : 'Deploy now'}
          </Button>
        </>
      ) : undefined}
      onDismiss={showFailure ? dismissFailure : undefined}
    >
      {open && pending && (
        <ul className="mt-2 space-y-1">
          {pending.commits.map((c) => (
            <li key={c.sha} className="flex gap-2 font-mono text-xs">
              <span className="text-warning">{c.sha.slice(0, 7)}</span>
              <span className="flex-1 truncate">{c.subject}</span>
              <span className="text-ink-muted">{c.author}</span>
            </li>
          ))}
        </ul>
      )}
      {showFailure && (
        <div className="mt-2 space-y-1 border-t border-edge pt-2">
          {showError && <p className="text-xs text-danger">{deploy?.error}</p>}
          {failed.length > 0 && (
            <p className="mt-1 text-xs text-danger">
              {failed.length} session{failed.length === 1 ? '' : 's'} failed to resume — autopilot is
              recovering {failed.length === 1 ? 'it' : 'them'}…{' '}
              <span className="font-mono">{failed.join(', ')}</span>
            </p>
          )}
          {gaveUp.length > 0 && (
            <p className="mt-1 text-xs text-danger">
              {gaveUp.length} session{gaveUp.length === 1 ? '' : 's'} couldn’t be recovered — open to
              retry or archive:{' '}
              <span className="font-mono">{gaveUp.join(', ')}</span>
            </p>
          )}
        </div>
      )}
    </Banner>
  )
}
