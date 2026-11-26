import * as React from 'react'
import { MonitorOff } from 'lucide-react'
import { KIOSK_EXCHANGE_PATH } from '@/shell/kiosk'
import { EmptyState } from '@/ui/EmptyState'

const EXCHANGE_URL = '/api/kiosk/session'
/** Where the server's 303 lands, if a test double or proxy does not follow it. */
const FALLBACK_LANDING = '/pipeline?kiosk=1'

type Phase = 'exchanging' | 'missing' | 'refused' | 'failed'

/**
 * `/kiosk?token=…` — redeem a display link for a read-only display session
 * (#1408 FR-G10.3).
 *
 * The token is scrubbed from the visible URL (and so from history, a reload
 * and any Referer) BEFORE anything else happens, then posted as a form body to
 * `POST /api/kiosk/session`, which answers with an HttpOnly display cookie and
 * a 303 to the pipeline wall. A `fetch` rather than a native form submit so a
 * revoked or expired link can render a message here instead of the browser
 * showing a raw JSON 401; the browser still sends `Origin` (the exchange is
 * Origin-guarded) and stores the same-origin cookie.
 *
 * The landing is a full navigation, not a router push: the shell reads kiosk
 * mode once at mount, and it must mount under the new cookie.
 */
export function KioskExchange() {
  // Read once, at mount — the effect below removes it from the URL.
  const [token] = React.useState(() => new URLSearchParams(window.location.search).get('token'))
  const [phase, setPhase] = React.useState<Phase>(token ? 'exchanging' : 'missing')
  // StrictMode runs mount effects twice; a token is redeemed once per visit.
  const started = React.useRef(false)

  React.useEffect(() => {
    if (started.current) return
    started.current = true
    window.history.replaceState(window.history.state, '', KIOSK_EXCHANGE_PATH)
    if (!token) return
    void (async () => {
      try {
        const res = await fetch(EXCHANGE_URL, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ token }).toString(),
        })
        if (!res.ok) {
          setPhase(res.status === 401 || res.status === 403 ? 'refused' : 'failed')
          return
        }
        window.location.replace(res.redirected ? res.url : FALLBACK_LANDING)
      } catch {
        setPhase('failed')
      }
    })()
  }, [token])

  if (phase === 'exchanging') {
    return <div role="status" className="p-6 text-sm text-ink-muted">Opening display…</div>
  }
  const copy: Record<Exclude<Phase, 'exchanging'>, { title: string; description: string }> = {
    missing: {
      title: 'This display link is incomplete',
      description: 'Open the full link copied from Settings › Display links.',
    },
    refused: {
      title: 'This display link has expired or been revoked',
      description: 'Create a new one in Settings › Display links.',
    },
    failed: {
      title: 'Could not open the display',
      description: 'The server could not be reached. Open the display link again to retry.',
    },
  }
  return (
    <div role="alert">
      <EmptyState icon={<MonitorOff size={32} aria-hidden="true" />} {...copy[phase]} />
    </div>
  )
}
