import { useState } from 'react'
import { KeyRound, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useCredentialAlerts, useDismissCredentialAlert } from '@/hooks/useCredentialAlerts'

/**
 * App-level banner: when a ccswitch clone can no longer fetch because its git
 * credentials are stale (issue #670), surface a prominent, actionable warning
 * with a link to refresh the token. Renders nothing until at least one repo is
 * flagged. Backlog Runner pauses those repos server-side until a fetch
 * succeeds, at which point the alert auto-clears and this banner disappears.
 *
 * Dismiss is client-local (this session) — the underlying pause persists until
 * the credential actually works, so hiding the banner never hides the fix.
 */
export function GitCredentialBanner() {
  const { data: alerts } = useCredentialAlerts()
  const dismissAlert = useDismissCredentialAlert()
  // Repos the user hid this session (keyed by repo). Server auto-clear wins over
  // this — a repo that recovers drops out of `alerts` regardless.
  const [hidden, setHidden] = useState<Set<string>>(new Set())

  const visible = (alerts ?? []).filter((a) => !hidden.has(a.repo))
  if (visible.length === 0) return null

  return (
    <div
      role="alert"
      className={cn(
        'fixed inset-x-0 top-0 z-50 flex flex-col gap-1',
        'border-b border-rose-300 dark:border-rose-700 bg-rose-50 dark:bg-rose-900/90 px-4 py-2',
      )}
    >
      {visible.map((alert) => (
        <div key={alert.repo} className="flex items-center justify-center gap-3">
          <KeyRound
            size={16}
            className="text-rose-700 dark:text-rose-300 flex-shrink-0"
            aria-hidden="true"
          />
          <span className="text-sm font-medium text-rose-900 dark:text-rose-100">
            Git credential refresh needed for{' '}
            <span className="font-mono">{alert.repo}</span> — backlog paused for this repo
          </span>
          <a
            href={alert.settingsUrl}
            target="_blank"
            rel="noreferrer"
            className={cn(
              'rounded-md bg-rose-600 px-3 py-1 text-xs font-semibold text-white',
              'hover:bg-rose-700 focus:outline-none focus:ring-2 focus:ring-rose-400',
            )}
          >
            Refresh token
          </a>
          <button
            type="button"
            aria-label={`Dismiss credential alert for ${alert.repo}`}
            onClick={() => {
              setHidden((prev) => new Set(prev).add(alert.repo))
              dismissAlert.mutate(alert.repo)
            }}
            className={cn(
              'rounded-md p-1 text-rose-700 dark:text-rose-300',
              'hover:bg-rose-100 dark:hover:bg-rose-800 focus:outline-none focus:ring-2 focus:ring-rose-400',
            )}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
      ))}
    </div>
  )
}
