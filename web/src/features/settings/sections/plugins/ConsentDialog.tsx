// The consent card (D-16c): before an install or update the owner sees what the plugin
// can run, where it comes from, and the verbatim warning. Only the button here posts,
// and it always posts `confirm: true` — the server refuses an install without it.
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { useInstallPlugin, useReloadPlugins, useUpdatePlugin } from '@/api/pluginHooks'
import { useState } from 'react'
import type { PluginReloadResult } from '@/api/types'
import { HostOutcomes } from './HostOutcomes'
import { SessionOutcomes } from './SessionOutcomes'
import { useOpRecord } from './useOpRecord'
import { CONSENT_COPY, CONTENTS_UNKNOWN, RELOAD_HELP, RELOAD_LABEL, RESTART_COPY, clip, refusalMessage } from './pluginText'

export interface ConsentTarget {
  mode: 'install' | 'update'
  pluginId: string
  name: string
  version?: string | null
  marketplace: string
  /** The marketplace source shown verbatim, or null when this page doesn't know it. */
  source: string | null
  /** "2 hooks · 1 MCP servers", or null when unknown. */
  runs: string | null
  reviewed?: boolean
}

export function ConsentDialog({ target, onClose }: { target: ConsentTarget; onClose: () => void }) {
  const install = useInstallPlugin()
  const update = useUpdatePlugin()
  const op = useOpRecord(target.mode === 'install' ? install : update)
  const reload = useReloadPlugins()
  const [reloadSessions, setReloadSessions] = useState(false)
  const [reloaded, setReloaded] = useState<PluginReloadResult | null>(null)
  const verb = target.mode === 'install' ? 'Install' : 'Update'
  // Zero hosts answering is not done: nothing changed, so the action stays offered.
  const done = op.record !== null && (op.record.result.hosts ?? []).length > 0
  // While the op is in flight the card is the only place its per-host outcome lands:
  // closing it (Cancel, Escape, backdrop) would lose the result, and couldn't stop the op.
  const busy = op.pending || reload.isPending
  const close = () => { if (!busy) onClose() }
  return (
    <Dialog open onClose={close} dismissable={!busy} title={`${verb} plugin`} size="lg">
      <div className="space-y-3 text-sm min-w-0">
        <div className="min-w-0">
          <div className="font-medium break-all"><bdi>{clip(target.name, 120)}</bdi></div>
          <div className="text-xs opacity-70 break-all">
            <bdi>{clip(target.pluginId, 200)}</bdi>{target.version ? ` · ${clip(target.version, 40)}` : ''}
            {target.reviewed ? ' · Reviewed by Anthropic' : ''}
          </div>
        </div>
        <dl className="space-y-2">
          <div>
            <dt className="text-xs opacity-60">What it can run</dt>
            <dd>{target.runs ?? CONTENTS_UNKNOWN}</dd>
          </div>
          <div>
            <dt className="text-xs opacity-60">Marketplace source</dt>
            <dd className="break-all">
              <code><bdi>{target.source ?? `${clip(target.marketplace, 80)} (source not shown by this host)`}</bdi></code>
            </dd>
          </div>
        </dl>
        <p className="rounded-md border border-warning p-2 text-warning">{CONSENT_COPY}</p>
        {op.error && <p role="alert" className="text-xs text-danger break-words">{op.error}</p>}
        {op.record && <HostOutcomes record={op.record} retrying={op.pending} />}
        {reloaded && <SessionOutcomes result={reloaded} />}
        {reload.isError && (
          <p role="alert" className="text-xs text-danger break-words">
            Couldn’t reload running sessions: {refusalMessage(reload.error)}
          </p>
        )}
        {!done && (
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              aria-label={RELOAD_LABEL}
              checked={reloadSessions}
              disabled={busy}
              onChange={(e) => setReloadSessions(e.target.checked)}
            />
            <span className="min-w-0"><span className="font-medium">{RELOAD_LABEL}</span> — {RELOAD_HELP}</span>
          </label>
        )}
        <div className="flex justify-end gap-2 flex-wrap">
          <Button variant="secondary" size="lg" disabled={busy} onClick={close}>{done ? 'Close' : 'Cancel'}</Button>
          {!done && (
            <Button
              size="lg"
              loading={busy}
              onClick={() => op.run(target.pluginId, `${verb} ${target.name}`, {
                note: RESTART_COPY,
                onSuccess: (result) => {
                  if (!reloadSessions || !(result.ok || result.hosts.some((h) => h.outcome === 'ok'))) return
                  reload.mutate({ holdOnCacheImpact: true }, { onSuccess: setReloaded })
                },
              })}
            >
              {verb}
            </Button>
          )}
        </div>
      </div>
    </Dialog>
  )
}
