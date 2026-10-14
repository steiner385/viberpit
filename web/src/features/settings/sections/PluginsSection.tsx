// web/src/features/settings/sections/PluginsSection.tsx
import { useEffect, useState } from 'react'
import { useLibraryPluginDetail, useLibraryPlugins, useTogglePlugin } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { LibraryPlugin } from '@/api/types'

export function PluginsSection() {
  const { data, isLoading } = useLibraryPlugins()
  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>
  const plugins = data?.plugins ?? []
  const marketplaces = data?.marketplaces ?? []
  return (
    <div>
      <p className="text-xs opacity-60 mb-3">
        Plugins are shared across all accounts. Enable/disable applies to newly started sessions.
      </p>
      <ul className="space-y-2">
        {plugins.map((p) => <PluginRow key={p.id} plugin={p} />)}
        {plugins.length === 0 && (
          <li className="text-sm opacity-70 p-4">No plugins installed.</li>
        )}
      </ul>
      {marketplaces.length > 0 && (
        <div className="mt-6 text-xs opacity-60">
          Marketplaces: {marketplaces.map((m) => m.name).join(', ')}
        </div>
      )}
    </div>
  )
}

function PluginRow({ plugin }: { plugin: LibraryPlugin }) {
  const [expanded, setExpanded] = useState(false)
  const toggle = useTogglePlugin()
  // A save can take a minute: the server snapshots the shared config first (#3067). Show the
  // requested state at once with a saving status, hold it until the refetch agrees, and on
  // failure fall back to the stored state with the reason.
  const requested = toggle.variables?.enabled
  const saving = toggle.isPending
  const awaitingRefetch = toggle.isSuccess && requested !== plugin.enabled
  const shown = (saving || awaitingRefetch) && requested !== undefined ? requested : plugin.enabled
  useEffect(() => {
    if (toggle.isSuccess && requested === plugin.enabled) toggle.reset()
  }, [toggle, requested, plugin.enabled])
  const saveError = toggle.isError
    ? extractMutationErrorMessage(
        toggle.error,
        toggle.error instanceof Error && toggle.error.message ? toggle.error.message : undefined,
      )
    : null
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
    <li className="border border-edge rounded-lg p-3">
      <div className="flex items-center gap-3">
        <button
          className="flex-1 min-w-0 text-left"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          <div className="flex items-baseline gap-2 min-w-0">
            <span className="font-medium truncate">{plugin.name}</span>
            {plugin.version && <span className="text-xs opacity-60">{plugin.version}</span>}
            {plugin.category && (
              <span className="text-xs px-1.5 py-0.5 rounded-full border border-edge opacity-70">
                {plugin.category}
              </span>
            )}
          </div>
          {plugin.description && (
            <p className="text-sm opacity-70 truncate">{plugin.description}</p>
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
            onChange={(e) => toggle.mutate({ id: plugin.id, enabled: e.target.checked })}
          />
        </label>
      </div>
      {saveError && (
        <p role="alert" className="mt-2 text-xs text-danger">
          Couldn't save {plugin.name}: {saveError}
        </p>
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
              <div className="text-xs opacity-50">{plugin.marketplace} · updated {plugin.lastUpdated ?? 'unknown'}</div>
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
    <div>
      <span className="font-medium">{label}:</span>{' '}
      <span className="opacity-80">{items.join(', ')}</span>
    </div>
  )
}
