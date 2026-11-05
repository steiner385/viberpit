// web/src/features/settings/sections/PluginsSection.tsx
//
// Settings › Plugins (D-16c). Lazy: the marketplace surfaces are their own chunk.
// Claude only — the section lives in the Claude config group, and the Codex library is
// unsupported (`CODEX_LIBRARY_UNSUPPORTED_DETAIL`). Every change fans out to every host
// (D2) and reports per-host outcomes; install, update, add and confirm hide while the
// `installEnabled` kill switch is off, while enable/disable, uninstall and remove stay.
import { useEffect, useState } from 'react'
import { useLibraryPluginDetail, useLibraryPlugins, useTogglePlugin, useUninstallPlugin } from '@/api/pluginHooks'
import type { LibraryMarketplace, LibraryPlugin } from '@/api/types'
import { Button } from '@/ui/Button'
import { useConfirm } from '@/ui/useConfirm'
import { BrowsePanel } from './plugins/BrowsePanel'
import { ConsentDialog, type ConsentTarget } from './plugins/ConsentDialog'
import { HostOutcomes, type OpRecord } from './plugins/HostOutcomes'
import { MarketplacesPanel } from './plugins/MarketplacesPanel'
import { useOpRecord } from './plugins/useOpRecord'
import { INSTALL_OFF_COPY, clip, marketplaceSourceText, refusalMessage, runnableSummary } from './plugins/pluginText'

export default function PluginsSection() {
  const { data, isLoading } = useLibraryPlugins()
  const [consent, setConsent] = useState<ConsentTarget | null>(null)
  const uninstall = useOpRecord(useUninstallPlugin())
  const { confirm, dialog } = useConfirm()
  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>
  const plugins = data?.plugins ?? []
  const marketplaces = data?.marketplaces ?? []
  // Absent on an older server: treat as on, the server still enforces the switch.
  const installEnabled = data?.installEnabled !== false

  async function onUninstall(p: LibraryPlugin) {
    const ok = await confirm({
      title: 'Uninstall plugin',
      description: `Uninstall ${clip(p.name, 80)} on every host? Its data is kept.`,
      confirmLabel: 'Uninstall',
      tone: 'danger',
    })
    if (ok) uninstall.run(p.id, `Uninstall ${clip(p.name, 80)}`)
  }

  return (
    <div className="min-w-0">
      <p className="text-xs opacity-60 mb-3">
        Plugins are shared across all accounts and installed on every host. Changes apply to newly started sessions.
      </p>
      {!installEnabled && (
        <p className="mb-3 rounded-md border border-edge p-2 text-sm" aria-label="Plugin installs off">{INSTALL_OFF_COPY}</p>
      )}
      {uninstall.error && <p role="alert" className="mb-2 text-xs text-danger break-words">{uninstall.error}</p>}
      {uninstall.record && (
        <div className="mb-2"><HostOutcomes record={uninstall.record} retrying={uninstall.pending} onDismiss={uninstall.clear} /></div>
      )}
      <ul className="space-y-2">
        {plugins.map((p) => (
          <PluginRow
            key={p.id}
            plugin={p}
            installEnabled={installEnabled}
            busy={uninstall.pending}
            onUpdate={() => setConsent(updateTarget(p, marketplaces))}
            onUninstall={() => void onUninstall(p)}
          />
        ))}
        {plugins.length === 0 && (
          <li className="text-sm opacity-70 p-4">No plugins installed.</li>
        )}
      </ul>
      <MarketplacesPanel marketplaces={marketplaces} installEnabled={installEnabled} />
      {installEnabled && <BrowsePanel marketplaces={marketplaces} onInstall={setConsent} />}
      {consent && <ConsentDialog target={consent} onClose={() => setConsent(null)} />}
      {dialog}
    </div>
  )
}

function updateTarget(p: LibraryPlugin, marketplaces: LibraryMarketplace[]): ConsentTarget {
  return {
    mode: 'update',
    pluginId: p.id,
    name: p.name,
    version: p.version,
    marketplace: p.marketplace,
    source: marketplaceSourceText(marketplaces.find((m) => m.name === p.marketplace)),
    runs: runnableSummary(p.components),
  }
}

function PluginRow({ plugin, installEnabled, busy, onUpdate, onUninstall }: {
  plugin: LibraryPlugin
  installEnabled: boolean
  busy: boolean
  onUpdate: () => void
  onUninstall: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const toggle = useTogglePlugin()
  const [toggleRecord, setToggleRecord] = useState<OpRecord | null>(null)
  // A save can take a minute: the server snapshots the shared config first (#3067). Show the
  // requested state at once with a saving status, hold it until the refetch agrees, and on
  // failure fall back to the stored state with the reason. A partial failure (`ok: false`)
  // stops holding too: the per-host outcome says where it didn't apply.
  const requested = toggle.variables?.enabled
  const saving = toggle.isPending
  const awaitingRefetch = toggle.isSuccess && toggle.data?.ok !== false && requested !== plugin.enabled
  const shown = (saving || awaitingRefetch) && requested !== undefined ? requested : plugin.enabled
  useEffect(() => {
    if (toggle.isSuccess && requested === plugin.enabled) toggle.reset()
  }, [toggle, requested, plugin.enabled])
  const saveError = toggle.isError ? refusalMessage(toggle.error) : null
  const setEnabled = function go(enabled: boolean) {
    toggle.mutate({ id: plugin.id, enabled }, {
      onSuccess: (result) => setToggleRecord({
        label: `${enabled ? 'Enable' : 'Disable'} ${clip(plugin.name, 80)}`,
        result,
        retry: () => go(enabled),
      }),
      onError: () => setToggleRecord(null),
    })
  }
  const detail = useLibraryPluginDetail(plugin.id, expanded)
  const c = plugin.components
  const componentSummary = [
    c.commands && `${c.commands} commands`,
    c.skills && `${c.skills} skills`,
    c.agents && `${c.agents} agents`,
    c.mcpServers && `${c.mcpServers} MCP`,
    c.hooks && `${c.hooks} hooks`,
  ].filter(Boolean).join(' · ')

  return (
    <li className="border border-edge rounded-lg p-3 min-w-0">
      <div className="flex items-center gap-3 min-w-0">
        <button
          className="flex-1 min-w-0 text-left"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          <div className="flex items-baseline gap-2 min-w-0">
            <span className="font-medium truncate"><bdi>{clip(plugin.name, 120)}</bdi></span>
            {plugin.version && <span className="text-xs opacity-60">{clip(plugin.version, 40)}</span>}
            {plugin.category && (
              <span className="text-xs px-1.5 py-0.5 rounded-full border border-edge opacity-70 truncate">
                {clip(plugin.category, 40)}
              </span>
            )}
            {plugin.autoUpdate && <span className="text-xs opacity-60 shrink-0">auto-updates</span>}
          </div>
          {plugin.description && (
            <p className="text-sm opacity-70 truncate"><bdi>{clip(plugin.description, 500)}</bdi></p>
          )}
          {componentSummary && <p className="text-xs opacity-50 mt-0.5">{componentSummary}</p>}
          {plugin.tokenCost && (
            <span className="text-xs opacity-50">≈{plugin.tokenCost.alwaysOn} tok always-on</span>
          )}
        </button>
        <label className={`flex items-center gap-2 shrink-0 min-h-target ${saving ? 'cursor-wait' : 'cursor-pointer'}`}>
          <span role="status" className={`text-xs ${saving ? 'text-warning' : 'opacity-60'}`}>
            {saving ? 'Saving… (can take up to a minute)' : shown ? 'On' : 'Off'}
          </span>
          <input
            type="checkbox"
            role="switch"
            aria-label={`Enable ${plugin.name}`}
            checked={shown}
            disabled={saving}
            onChange={(e) => setEnabled(e.target.checked)}
          />
        </label>
      </div>
      <div className="mt-2 flex gap-2 flex-wrap">
        {installEnabled && (
          <Button size="sm" variant="secondary" disabled={busy} onClick={onUpdate} aria-label={`Update ${plugin.name}`}>
            Update
          </Button>
        )}
        <Button size="sm" variant="ghost" disabled={busy} onClick={onUninstall} aria-label={`Uninstall ${plugin.name}`}>
          Uninstall
        </Button>
      </div>
      {saveError && (
        <p role="alert" className="mt-2 text-xs text-danger">
          Couldn't save {plugin.name}: {saveError}
        </p>
      )}
      {toggleRecord && (
        <HostOutcomes record={toggleRecord} retrying={saving} onDismiss={() => setToggleRecord(null)} />
      )}
      {expanded && (
        <div className="mt-3 pt-3 border-t border-edge text-sm space-y-2">
          {detail.isLoading && <div className="opacity-70">Loading components…</div>}
          {detail.data && (
            <>
              <ComponentList label="Commands" items={detail.data.commands.map((x) => `/${x.path.replace(/\//g, ':')}`)} />
              <ComponentList label="Skills" items={detail.data.skills.map((x) => x.name)} />
              <ComponentList label="Agents" items={detail.data.agents} />
              <ComponentList label="MCP servers" items={detail.data.mcpServers.map((x) => `${x.name} (${x.transport})`)} />
              {detail.data.hasHooks && <div className="opacity-70">Provides lifecycle hooks.</div>}
              <div className="text-xs opacity-50 break-all">
                <bdi>{clip(plugin.marketplace, 120)}</bdi> · updated {plugin.lastUpdated ?? 'unknown'}
              </div>
            </>
          )}
        </div>
      )}
    </li>
  )
}

function ComponentList({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <div className="break-words">
      <span className="font-medium">{label}:</span>{' '}
      <span className="opacity-80">{items.join(', ')}</span>
    </div>
  )
}
