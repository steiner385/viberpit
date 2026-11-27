/**
 * SecuritySection — the caller's passkeys: list, add, revoke (#1410 FR-G12.12).
 *
 * Only for a full-scope session on a server with passkeys turned on
 * (`/api/config.webauthnEnabled`): a display session never sees it, and the
 * `/api/auth/credentials` routes do not exist while passkeys are off, so
 * nothing is fetched until both are known. Revoking a passkey also signs out
 * every session it started, so it asks first.
 */
import { useState } from 'react'
import { useAuthMe } from '@/api/auth'
import { ApiRequestError } from '@/api/client'
import { useConfig } from '@/api/hooks'
import { usePasskeys, useEnrolPasskey, useRevokePasskey } from '@/api/passkeys'
import { passkeysSupported, type PasskeyCredential } from '@/api/webauthn'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { formatEventTime } from '@/lib/formatBlockTime'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { useConfirm } from '@/ui/useConfirm'

const UNNAMED = 'Unnamed passkey'

function When({ iso }: { iso: string | null }) {
  if (!iso) return <span className="text-ink-muted">Never</span>
  const t = formatEventTime(iso)
  return <time dateTime={iso} title={t.full}>{t.short}</time>
}

function enrolFailure(err: unknown): string {
  if (err instanceof DOMException && err.name === 'NotAllowedError') {
    return 'The passkey was not saved. The prompt was cancelled or timed out.'
  }
  if (err instanceof DOMException && err.name === 'InvalidStateError') {
    return 'This device already has a passkey for your account.'
  }
  if (err instanceof ApiRequestError && err.status === 403) {
    // A passkey session may enrol only right after its sign-in (server: PASSKEY_REAUTH_REQUIRED).
    const detail = err.body.detail as { code?: unknown } | undefined
    if (detail?.code === 'PASSKEY_REAUTH_REQUIRED') {
      return 'Sign in again with your passkey, then add the new one.'
    }
    return 'You cannot add a passkey from this session.'
  }
  return extractMutationErrorMessage(err, 'Could not add the passkey.')
}

export function SecuritySection() {
  const me = useAuthMe()
  const config = useConfig()
  if (me.isPending || me.data?.scope === 'display' || config.isPending) return null
  if (!config.data?.webauthnEnabled) {
    return <p className="text-sm text-ink-muted">Passkey sign-in is not turned on for this server.</p>
  }
  return <Passkeys />
}

function Passkeys() {
  const passkeys = usePasskeys()
  const enrol = useEnrolPasskey()
  const revoke = useRevokePasskey()
  const { confirm, dialog } = useConfirm()
  const [label, setLabel] = useState('')
  const supported = passkeysSupported()

  const add = () => {
    enrol.mutate(label, { onSuccess: () => setLabel('') })
  }

  const askRevoke = async (p: PasskeyCredential) => {
    const name = p.label || UNNAMED
    const ok = await confirm({
      title: `Revoke ${name}?`,
      description: 'It can no longer sign in, and anyone signed in with it is signed out.',
      confirmLabel: 'Revoke passkey',
    })
    if (ok) revoke.mutate(p.id)
  }

  return (
    <div className="space-y-6">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => { e.preventDefault(); add() }}
      >
        <Field label="Name" htmlFor="passkey-label" className="min-w-48 flex-1"
               help="Which device or key this is, e.g. “Work laptop”.">
          <Input id="passkey-label" value={label} maxLength={80}
                 onChange={(e) => setLabel(e.target.value)} />
        </Field>
        <Button type="submit" loading={enrol.isPending} disabled={!supported || enrol.isPending}>
          Add a passkey
        </Button>
      </form>
      {!supported && <p className="text-sm text-ink-muted">This browser does not support passkeys.</p>}
      {enrol.isError && <p role="alert" className="text-sm text-danger">{enrolFailure(enrol.error)}</p>}
      {revoke.isError && (
        <p role="alert" className="text-sm text-danger">
          {extractMutationErrorMessage(revoke.error, 'Could not revoke the passkey.')}
        </p>
      )}

      {passkeys.isPending ? (
        <p className="text-sm text-ink-muted">Loading passkeys…</p>
      ) : passkeys.isError ? (
        <p role="alert" className="text-sm text-danger">Could not load passkeys.</p>
      ) : passkeys.data.length === 0 ? (
        <p className="text-sm text-ink-muted">No passkeys yet.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-ink-muted">
            <tr>
              <th scope="col" className="py-1 pr-3 font-medium">Name</th>
              <th scope="col" className="py-1 pr-3 font-medium">Added</th>
              <th scope="col" className="py-1 pr-3 font-medium">Last used</th>
              <th scope="col" className="py-1"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {passkeys.data.map((p) => (
              <tr key={p.id} className="border-t border-edge">
                <td className="py-2 pr-3 text-ink">{p.label || UNNAMED}</td>
                <td className="py-2 pr-3"><When iso={p.createdAt} /></td>
                <td className="py-2 pr-3"><When iso={p.lastUsedAt} /></td>
                <td className="py-2 text-right">
                  <Button type="button" size="sm" variant="secondary"
                          disabled={revoke.isPending && revoke.variables === p.id}
                          aria-label={`Revoke ${p.label || UNNAMED}`}
                          onClick={() => { void askRevoke(p) }}>
                    Revoke
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {dialog}
    </div>
  )
}
