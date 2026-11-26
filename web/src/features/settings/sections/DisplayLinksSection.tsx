/**
 * DisplayLinksSection — mint, list and revoke kiosk display links (#1408 FR-G10.9).
 *
 * A display link opens `/kiosk?token=…`, which trades the token for a
 * read-only display session: the pipeline wall and nothing else, enforced by
 * the server. The secret is shown ONCE, right after minting; the list shows
 * only metadata. Revoking ends a live display on its next poll.
 */
import { useState } from 'react'
import { useAuthMe } from '@/api/auth'
import { useDisplayTokens, useMintDisplayToken, useRevokeToken } from '@/api/displayTokens'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { formatEventTime } from '@/lib/formatBlockTime'
import { displayLinkFor } from '@/shell/kiosk'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'

const DEFAULT_TTL_DAYS = 30

function When({ iso }: { iso: string | null }) {
  if (!iso) return <span className="text-ink-muted">Never</span>
  const t = formatEventTime(iso)
  return <time dateTime={iso} title={t.full}>{t.short}</time>
}

export function DisplayLinksSection() {
  // A display session never reaches Settings (the shell keeps it on the
  // pipeline and the server 403s these routes), but the list must not render
  // for one even if it did — nor fetch the list before the scope is known.
  const me = useAuthMe()
  if (me.isPending || me.data?.scope === 'display') return null
  return <DisplayLinks />
}

function DisplayLinks() {
  const tokens = useDisplayTokens()
  const mint = useMintDisplayToken()
  const revoke = useRevokeToken()
  const [label, setLabel] = useState('')
  const [ttlDays, setTtlDays] = useState(String(DEFAULT_TTL_DAYS))
  const [minted, setMinted] = useState<{ label: string; link: string } | null>(null)
  const [copied, setCopied] = useState(false)

  const create = () => {
    setCopied(false)
    mint.mutate({ label: label.trim(), ttlDays: Number(ttlDays) }, {
      onSuccess: (row) => {
        setMinted({ label: row.label ?? '', link: displayLinkFor(row.token) })
        setLabel('')
      },
    })
  }

  return (
    <div className="space-y-6">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => { e.preventDefault(); create() }}
      >
        <Field label="Label" htmlFor="display-link-label" className="min-w-48 flex-1"
               help="Where the screen is, e.g. “Kitchen TV”.">
          <Input id="display-link-label" value={label} required maxLength={80}
                 onChange={(e) => setLabel(e.target.value)} />
        </Field>
        <Field label="Expires after (days)" htmlFor="display-link-ttl" className="w-40">
          <Input id="display-link-ttl" type="number" min={1} inputMode="numeric" value={ttlDays}
                 onChange={(e) => setTtlDays(e.target.value)} />
        </Field>
        <Button type="submit" disabled={mint.isPending || label.trim() === ''}>Create link</Button>
      </form>
      {mint.isError && (
        <p role="alert" className="text-sm text-danger">
          {extractMutationErrorMessage(mint.error, 'Could not create the display link.')}
        </p>
      )}

      {minted && (
        <div className="space-y-2 rounded-md border border-edge bg-surface-raised p-3" data-testid="display-link-minted">
          <p className="text-sm text-ink">
            Link for <span className="font-medium">{minted.label}</span>. Open it on the display;
            it is shown only once.
          </p>
          <div className="flex gap-2">
            <Input readOnly aria-label="Display link" value={minted.link} onFocus={(e) => e.currentTarget.select()} />
            <Button type="button" variant="secondary"
                    onClick={() => { void navigator.clipboard?.writeText(minted.link).then(() => setCopied(true)) }}>
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>
        </div>
      )}

      {tokens.isPending ? (
        <p className="text-sm text-ink-muted">Loading display links…</p>
      ) : tokens.isError ? (
        <p role="alert" className="text-sm text-danger">Could not load display links.</p>
      ) : tokens.data.length === 0 ? (
        <p className="text-sm text-ink-muted">No display links yet.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-ink-muted">
            <tr>
              <th scope="col" className="py-1 pr-3 font-medium">Label</th>
              <th scope="col" className="py-1 pr-3 font-medium">Created</th>
              <th scope="col" className="py-1 pr-3 font-medium">Expires</th>
              <th scope="col" className="py-1 pr-3 font-medium">Last used</th>
              <th scope="col" className="py-1"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {tokens.data.map((t) => (
              <tr key={t.id} className="border-t border-edge">
                <td className="py-2 pr-3 text-ink">{t.label || 'Unlabelled'}</td>
                <td className="py-2 pr-3"><When iso={t.createdAt} /></td>
                <td className="py-2 pr-3"><When iso={t.expiresAt} /></td>
                <td className="py-2 pr-3"><When iso={t.lastUsedAt} /></td>
                <td className="py-2 text-right">
                  <Button type="button" size="sm" variant="secondary"
                          disabled={revoke.isPending && revoke.variables === t.id}
                          aria-label={`Revoke ${t.label || 'display link'}`}
                          onClick={() => revoke.mutate(t.id)}>
                    Revoke
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
