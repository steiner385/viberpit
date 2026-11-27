import * as React from 'react'
import { useSearchParams } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import { passkeysSupported, startPasskeyLogin } from '@/api/webauthn'
import { safeNextPath } from '@/lib/loginRedirect'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'

/** What went wrong, in words a person can act on. */
function failureMessage(err: unknown): string {
  if (err instanceof ApiRequestError) {
    if (err.status === 401) return 'That passkey was not accepted.'
    if (err.status === 429) {
      return err.retryAfterS !== null
        ? `Too many attempts. Try again in ${err.retryAfterS} seconds.`
        : 'Too many attempts. Wait a minute and try again.'
    }
    if (err.status === 400) return 'That sign-in attempt expired. Try again.'
  }
  if (err instanceof DOMException && err.name === 'NotAllowedError') return 'Sign-in was cancelled or timed out.'
  return 'Could not sign in. Try again.'
}

/**
 * `/login` — passkey sign-in (#1410 FR-G12.12).
 *
 * Reached when an API call answers 401 with `login: "passkey"` (`api/client`).
 * Renders OUTSIDE the shell, like the display-link exchange: the shell polls
 * and opens `/ws` on mount, and every one of those calls would 401 here.
 *
 * The identity field is optional — empty, the browser offers every passkey it
 * holds for this site. On success the landing is a full navigation to `next`
 * (a same-origin path only), so the shell mounts under the new session cookie.
 */
export function LoginPage() {
  const [params] = useSearchParams()
  const next = safeNextPath(params.get('next'))
  const supported = passkeysSupported()
  const [identity, setIdentity] = React.useState('')
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  const signIn = async () => {
    setBusy(true)
    setError(null)
    try {
      await startPasskeyLogin(identity.trim() || undefined)
      window.location.replace(next)
    } catch (err) {
      setError(failureMessage(err))
      setBusy(false)
    }
  }

  return (
    <main className="flex min-h-dvh items-center justify-center bg-canvas p-6 text-ink">
      <div className="w-full max-w-sm space-y-6">
        <h1 className="text-xl font-semibold">Sign in</h1>
        <form
          className="space-y-4"
          onSubmit={(e) => { e.preventDefault(); void signIn() }}
        >
          <Field label="Email or username (optional)" htmlFor="login-identity"
                 help="Leave blank to choose from the passkeys saved on this device.">
            <Input id="login-identity" value={identity} autoComplete="username webauthn"
                   onChange={(e) => setIdentity(e.target.value)} />
          </Field>
          <Button type="submit" size="lg" className="w-full" loading={busy} disabled={!supported || busy}>
            Sign in with a passkey
          </Button>
        </form>
        {!supported && <p className="text-sm text-ink-muted">This browser does not support passkeys.</p>}
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      </div>
    </main>
  )
}
