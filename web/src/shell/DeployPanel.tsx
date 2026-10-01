// web/src/shell/DeployPanel.tsx
import { useId, useState } from 'react'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { useDeployState } from '@/lib/deployState'
import { useDeployController } from '@/lib/deployController'
import { DeployFailureNotice } from './DeployFailureNotice'
import { storageKey, migrateStorageNamespace } from '@/lib/storageKey'

// The failure notice is dismissed by CONTENT, not as a boolean: `gaveUp` is the daemon's
// live set and only changes when a human retries/archives, so a bare "hidden" flag would
// either mask the next failure or (if session-scoped) reappear on every reload — the
// original complaint. Persisting the signature of what was dismissed means the same
// failure stays gone across reloads while any new session id or error text brings the
// notice back on its own. Guarded like `lib/lastRepo.ts`: a blocked localStorage degrades
// to "not dismissed", never a crash.
const DISMISSED_KEY = storageKey('deployFailureDismissed')

function failureSignature(error: string | null, failed: string[], gaveUp: string[]): string {
  return JSON.stringify({ error, failed: [...failed].sort(), gaveUp: [...gaveUp].sort() })
}

function readDismissedSignature(): string | null {
  try {
    return window.localStorage.getItem(
      migrateStorageNamespace(window.localStorage, DISMISSED_KEY))
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
  const commitsId = useId()

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
      // Pending commits are information with a button, not a warning: on desktop
      // this banner sits above the session pane, and in amber it was the loudest
      // element on the page — louder than the needs-you states beneath it that
      // actually want the operator (2026-09-29 critique, P2). Amber is kept for
      // a failure, which is the case that genuinely needs a look.
      tone={showFailure ? 'warning' : 'info'}
      // Not a live region: commits, phase state and error text all change while a deploy
      // runs, and role="status" would announce every one of those to a screen reader.
      role="none"
      title={s.behind ? (
        // A disclosure, and the first Tab stop on most desktop pages: it says
        // so (`aria-expanded`, `aria-controls`) and meets the 44px floor. It
        // was a bare 183×20 button with no state (2026-10-01 shell critique, P2).
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-controls={open && s.count > 0 ? commitsId : undefined}
          className="inline-flex min-h-target items-center gap-1 rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {s.count} change{s.count === 1 ? '' : 's'} pending deploy <span aria-hidden="true">{open ? '▾' : '▸'}</span>
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
        <ul id={commitsId} className="mt-2 space-y-1">
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
