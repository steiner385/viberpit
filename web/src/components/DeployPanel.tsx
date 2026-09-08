// web/src/components/DeployPanel.tsx
import { useState } from 'react'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { useDeployState } from '@/lib/deployState'
import { useDeployController } from '@/lib/deployController'
import { DeployFailureNotice } from './DeployFailureNotice'

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

/**
 * The deploy state as a BANNER, for the app-level stack.
 *
 * Hidden on phone (`hideOn: ['phone']` in `shell/useAppBanners`) — it is the
 * tallest item in the stack and Settings › Deploy is its full-size home. The
 * state itself comes from `useDeployState`, shared with that section.
 */
export function DeployPanel() {
  const s = useDeployState()
  const { start, starting } = useDeployController()
  const [open, setOpen] = useState(false)

  // Only the failure notice is dismissible. Pending commits are actionable (Deploy button)
  // and clear on their own once deployed — hiding them would hide the only way to deploy.
  const [dismissedSig, setDismissedSig] = useState(readDismissedSignature)
  const failureSig = failureSignature(s.showError ? s.error : null, s.failed, s.gaveUp)
  const showFailure = s.hasFailure && dismissedSig !== failureSig
  const dismissFailure = () => {
    writeDismissedSignature(failureSig)
    setDismissedSig(failureSig)
  }

  if (!s.behind && !showFailure) return null

  return (
    <Banner
      tone="warning"
      // Not a live region: commits, phase state and error text all change while a deploy
      // runs, and role="status" would announce every one of those to a screen reader.
      role="none"
      title={s.behind ? (
        <button type="button" onClick={() => setOpen((o) => !o)}>
          {s.count} change{s.count === 1 ? '' : 's'} pending deploy {open ? '▾' : '▸'}
        </button>
      ) : undefined}
      actions={s.behind ? (
        <>
          {s.reason && <span className="text-xs text-warning">{s.reason}</span>}
          <Button
            size="sm"
            variant="secondary"
            disabled={!s.canDeploy || starting}
            onClick={() => start(s.deployTarget)}
          >
            {starting ? 'Starting…' : 'Deploy now'}
          </Button>
        </>
      ) : undefined}
      onDismiss={showFailure ? dismissFailure : undefined}
    >
      {open && s.count > 0 && (
        <ul className="mt-2 space-y-1">
          {s.commits.map((c) => (
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
          <DeployFailureNotice state={s} />
        </div>
      )}
    </Banner>
  )
}
