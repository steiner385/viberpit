// Marketplaces (D-16c, D1): only a marketplace the owner added or confirmed here, at the
// source shown, can be installed from. Add shows the exact source before an explicit
// Confirm; a marketplace configured outside ViberPit is listed unconfirmed with its
// source verbatim and a Confirm that echoes that source back.
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { QK } from '@/api/hooks'
import {
  useAddMarketplace, useConfirmMarketplace, useRemoveMarketplace, useUpdateMarketplace,
} from '@/api/pluginHooks'
import type { LibraryMarketplace } from '@/api/types'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Input } from '@/ui/Input'
import { useConfirm } from '@/ui/useConfirm'
import { HostOutcomes } from './HostOutcomes'
import { useOpRecord } from './useOpRecord'
import { clip, marketplaceSourceText, refusalMessage, refusalReason, SOURCE_CHANGED_COPY } from './pluginText'

const FETCH_WARNING = 'Every host fetches this source and runs plugins installed from it.'

export function MarketplacesPanel({ marketplaces, installEnabled }: {
  marketplaces: LibraryMarketplace[]
  installEnabled: boolean
}) {
  const update = useOpRecord(useUpdateMarketplace())
  const remove = useOpRecord(useRemoveMarketplace())
  const { confirm, dialog } = useConfirm()

  async function onRemove(m: LibraryMarketplace) {
    const ok = await confirm({
      title: 'Remove marketplace',
      description: `Remove ${clip(m.name, 80)} from every host? Plugins from it can no longer be installed or updated.`,
      confirmLabel: 'Remove',
      tone: 'danger',
    })
    if (ok) remove.run(m.name, `Remove ${clip(m.name, 80)}`)
  }

  return (
    <section aria-labelledby="plugin-marketplaces" className="mt-8">
      <h3 id="plugin-marketplaces" className="text-sm font-semibold mb-2">Marketplaces</h3>
      {installEnabled && <AddMarketplace />}
      <ul className="space-y-2 mt-3">
        {marketplaces.map((m) => (
          <MarketplaceRow
            key={m.name}
            marketplace={m}
            installEnabled={installEnabled}
            onUpdate={() => update.run(m.name, `Update ${clip(m.name, 80)}`)}
            onRemove={() => void onRemove(m)}
            busy={update.pending || remove.pending}
          />
        ))}
        {marketplaces.length === 0 && <li className="text-sm opacity-70">No marketplaces configured.</li>}
      </ul>
      {[update, remove].map((op, i) => (
        <div key={i}>
          {op.error && <p role="alert" className="mt-2 text-xs text-danger break-words">{op.error}</p>}
          {op.record && <HostOutcomes record={op.record} retrying={op.pending} onDismiss={op.clear} />}
        </div>
      ))}
      {dialog}
    </section>
  )
}

function AddMarketplace() {
  const op = useOpRecord(useAddMarketplace())
  const [draft, setDraft] = useState('')
  // The exact string shown for review; it is what gets posted, so an edit after Review
  // drops the review and asks again.
  const [reviewed, setReviewed] = useState<string | null>(null)
  return (
    <div className="rounded-lg border border-edge p-3">
      <form
        className="flex gap-2 flex-wrap"
        onSubmit={(e) => {
          e.preventDefault()
          const source = draft.trim()
          if (source) setReviewed(source)
        }}
      >
        <Input
          aria-label="Marketplace source"
          placeholder="owner/repo or https://host/path.git"
          value={draft}
          onChange={(e) => { setDraft(e.target.value); setReviewed(null) }}
          className="flex-1 min-w-0"
          autoComplete="off"
          spellCheck={false}
        />
        <Button type="submit" variant="secondary" disabled={!draft.trim()}>Review source</Button>
      </form>
      {reviewed !== null && (
        <div className="mt-3 space-y-2 text-sm" role="group" aria-label="Review marketplace source">
          <div>You are adding this source:</div>
          <code className="block rounded-md border border-edge p-2 break-all"><bdi>{reviewed}</bdi></code>
          <p className="text-warning">{FETCH_WARNING} Add only sources you trust.</p>
          <div className="flex gap-2 flex-wrap">
            <Button
              loading={op.pending}
              // A refusal keeps the input so the owner can fix it; an add clears it.
              onClick={() => op.run(reviewed, `Add ${clip(reviewed, 80)}`, {
                onSuccess: () => { setDraft(''); setReviewed(null) },
              })}
            >
              Confirm and add
            </Button>
            {/* An add in flight can't be called back; Cancel would only hide what it is adding. */}
            <Button variant="ghost" disabled={op.pending} onClick={() => setReviewed(null)}>Cancel</Button>
          </div>
        </div>
      )}
      {op.error && <p role="alert" className="mt-2 text-xs text-danger break-words">{op.error}</p>}
      {op.record && <HostOutcomes record={op.record} retrying={op.pending} onDismiss={op.clear} />}
    </div>
  )
}

function MarketplaceRow({ marketplace: m, installEnabled, onUpdate, onRemove, busy }: {
  marketplace: LibraryMarketplace
  installEnabled: boolean
  onUpdate: () => void
  onRemove: () => void
  busy: boolean
}) {
  const qc = useQueryClient()
  const confirmMarketplace = useConfirmMarketplace()
  const [error, setError] = useState<string | null>(null)
  const { confirm, dialog } = useConfirm()
  const source = marketplaceSourceText(m)
  const curation = m.curation ?? 'unconfirmed'
  const confirmable = typeof m.normalizedSource === 'string' && m.normalizedSource !== ''

  async function onConfirm() {
    const echoed = m.normalizedSource
    if (!echoed) return
    const ok = await confirm({
      title: curation === 'source_changed' ? 'Re-confirm marketplace' : 'Confirm marketplace',
      description: (
        <span className="block space-y-2">
          <span className="block">Allow installs from <bdi>{clip(m.name, 80)}</bdi> at this source:</span>
          <code className="block rounded-md border border-edge p-2 break-all"><bdi>{echoed}</bdi></code>
          <span className="block">{FETCH_WARNING}</span>
        </span>
      ),
      confirmLabel: 'Confirm',
      tone: 'warning',
    })
    if (!ok) return
    setError(null)
    confirmMarketplace.mutate({ name: m.name, source: echoed }, {
      onError: (e) => {
        setError(refusalReason(e) === 'source_changed' ? SOURCE_CHANGED_COPY : refusalMessage(e))
        void qc.invalidateQueries({ queryKey: QK.libraryPlugins })
      },
    })
  }

  return (
    <li className="border border-edge rounded-lg p-3 min-w-0">
      <div className="flex items-center gap-2 flex-wrap min-w-0">
        <span className="font-medium truncate min-w-0"><bdi>{clip(m.name, 120)}</bdi></span>
        {curation === 'confirmed' && <Badge tone="success">Confirmed</Badge>}
        {curation === 'unconfirmed' && <Badge tone="warning">Unconfirmed</Badge>}
        {curation === 'source_changed' && <Badge tone="danger">Source changed</Badge>}
        {m.autoUpdate && <span className="text-xs opacity-60">auto-updates</span>}
      </div>
      {curation === 'source_changed' && m.confirmedSource && (
        <div className="mt-1 text-xs min-w-0">
          <span className="opacity-60">Confirmed at </span>
          <code className="break-all"><bdi>{clip(m.confirmedSource)}</bdi></code>
        </div>
      )}
      <div className="mt-1 text-xs min-w-0">
        <span className="opacity-60">{curation === 'source_changed' ? 'Now points at ' : 'Source '}</span>
        <code className="break-all"><bdi>{source ?? 'unknown'}</bdi></code>
      </div>
      {curation !== 'confirmed' && !confirmable && (
        <p className="mt-1 text-xs opacity-70">
          This source can’t be confirmed here (a local, http or pinned source). Plugins from it can’t be installed from ViberPit.
        </p>
      )}
      {error && <p role="alert" className="mt-2 text-xs text-danger break-words">{error}</p>}
      <div className="mt-2 flex gap-2 flex-wrap">
        {installEnabled && curation !== 'confirmed' && confirmable && (
          <Button size="sm" loading={confirmMarketplace.isPending} onClick={() => void onConfirm()}>
            {curation === 'source_changed' ? 'Re-confirm' : 'Confirm'}
          </Button>
        )}
        {installEnabled && curation === 'confirmed' && (
          <Button size="sm" variant="secondary" disabled={busy} onClick={onUpdate}>Update</Button>
        )}
        <Button size="sm" variant="ghost" disabled={busy} onClick={onRemove} aria-label={`Remove ${m.name}`}>
          Remove
        </Button>
      </div>
      {dialog}
    </li>
  )
}
