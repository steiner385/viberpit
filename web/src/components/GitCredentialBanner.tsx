import { useState } from 'react'
import { X, KeyRound } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Banner } from '@/ui/Banner'
import { IconButton } from '@/ui/IconButton'
import { useCredentialAlerts, useDismissCredentialAlert } from '@/hooks/useCredentialAlerts'

/**
 * App-level banner: when a ccswitch clone can no longer fetch because its git
 * credentials are stale (issue #670), surface a prominent, actionable warning
 * linking to the page that actually fixes it. Renders nothing until at least one
 * repo is flagged. Backlog Runner pauses those repos server-side until a fetch
 * succeeds, at which point the alert auto-clears and this banner disappears.
 *
 * Two failure shapes, and the wording must match (issue #887): a personal access
 * token that expired, or a GitHub App installation that lost access. `viaApp`
 * comes from the server, which knows which credential the failing fetch used —
 * sending someone to Personal Access Tokens for an App failure is a dead end.
 *
 * Dismiss is client-local (this session) — the underlying pause persists until
 * the credential actually works, so hiding the banner never hides the fix.
 *
 * One `Banner` per alert (each independently role="alert") stacked inside a shared
 * fixed-to-top wrapper — several stale repos each get their own assertive announcement.
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
    <div className="fixed inset-x-0 top-0 z-50 flex flex-col gap-1 px-4 py-2">
      {visible.map((alert) => (
        <Banner
          key={alert.repo}
          tone="danger"
          icon={<KeyRound size={18} aria-hidden="true" />}
          className="border-x-0 border-t-0 rounded-none justify-center"
          title={
            <>
              {alert.viaApp ? 'GitHub App access needs attention for' : 'Git credential refresh needed for'}{' '}
              <span className="font-mono">{alert.repo}</span> — backlog paused for this repo
            </>
          }
          actions={
            <>
              <a
                href={alert.settingsUrl}
                target="_blank"
                rel="noreferrer"
                className={cn(
                  'rounded-md bg-danger-strong px-3 py-1 text-xs font-semibold text-white',
                  'hover:bg-danger-strong/90 focus:outline-none focus:ring-2 focus:ring-accent',
                )}
              >
                {alert.viaApp ? 'Review App access' : 'Refresh token'}
              </a>
              <IconButton
                aria-label={`Dismiss credential alert for ${alert.repo}`}
                size="sm"
                className="text-danger"
                onClick={() => {
                  setHidden((prev) => new Set(prev).add(alert.repo))
                  dismissAlert.mutate(alert.repo)
                }}
              >
                <X size={16} aria-hidden="true" />
              </IconButton>
            </>
          }
        />
      ))}
    </div>
  )
}
