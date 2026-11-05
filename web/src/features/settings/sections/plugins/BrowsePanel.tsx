// Browse (D-16c): installable plugins, searched and paged by the server. The server
// sends only confirmed-marketplace entries and REVIEWED Directory listings, so there is
// deliberately no client-side review filter here — the badge just reports `reviewed`.
import { useState } from 'react'
import { usePluginBrowse } from '@/api/pluginHooks'
import type { LibraryMarketplace, PluginBrowseEntry } from '@/api/types'
import { useDebouncedValue } from '@/lib/useDebouncedValue'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import type { ConsentTarget } from './ConsentDialog'
import { CONTENTS_UNKNOWN, DIRECTORY_MARKETPLACE, clip, marketplaceSourceText, runnableSummary } from './pluginText'

const PAGE = 20

export function BrowsePanel({ marketplaces, onInstall }: {
  marketplaces: LibraryMarketplace[]
  onInstall: (target: ConsentTarget) => void
}) {
  const [q, setQ] = useState('')
  const [marketplace, setMarketplace] = useState('')
  const [offset, setOffset] = useState(0)
  const debouncedQ = useDebouncedValue(q.trim().slice(0, 200), 300)
  const browse = usePluginBrowse({ q: debouncedQ, marketplace, offset, limit: PAGE })
  const data = browse.data
  const entries = data?.plugins ?? []
  const total = data?.total ?? 0
  const filterNames = [
    ...marketplaces.filter((m) => m.curation === 'confirmed').map((m) => m.name),
    DIRECTORY_MARKETPLACE,
  ].filter((n, i, all) => all.indexOf(n) === i)
  const byName = new Map(marketplaces.map((m) => [m.name, m]))

  return (
    <section aria-labelledby="plugin-browse" className="mt-8">
      <h3 id="plugin-browse" className="text-sm font-semibold mb-2">Browse</h3>
      <p className="text-xs opacity-60 mb-2">
        From marketplaces you confirmed, plus plugins Anthropic reviewed in its directory.
      </p>
      <div className="flex gap-2 flex-wrap">
        <Input
          type="search"
          aria-label="Search plugins"
          placeholder="Search plugins"
          value={q}
          maxLength={200}
          onChange={(e) => { setQ(e.target.value); setOffset(0) }}
          className="flex-1 min-w-0"
        />
        <Select
          aria-label="Marketplace"
          value={marketplace}
          onChange={(e) => { setMarketplace(e.target.value); setOffset(0) }}
          className="w-auto max-w-full"
        >
          <option value="">All marketplaces</option>
          {filterNames.map((n) => <option key={n} value={n}>{clip(n, 60)}</option>)}
        </Select>
      </div>
      {browse.isError && <p role="alert" className="mt-2 text-xs text-danger">Couldn’t load plugins.</p>}
      {browse.isLoading && <div className="mt-3 text-sm opacity-70">Loading…</div>}
      <ul className="mt-3 space-y-2" aria-busy={browse.isFetching || undefined}>
        {entries.map((e) => (
          <BrowseRow
            key={e.pluginId}
            entry={e}
            onInstall={() => onInstall({
              mode: 'install',
              pluginId: e.pluginId,
              name: e.name,
              version: e.version,
              marketplace: e.marketplace,
              source: marketplaceSourceText(byName.get(e.marketplace)),
              runs: runnableSummary(e.components),
              reviewed: e.reviewed,
            })}
          />
        ))}
        {data && entries.length === 0 && (
          <li className="text-sm opacity-70">{debouncedQ ? 'No plugins match.' : 'No plugins available.'}</li>
        )}
      </ul>
      {total > PAGE && (
        <div className="mt-2 flex items-center gap-2 text-xs">
          <Button size="sm" variant="secondary" disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</Button>
          <span className="opacity-70">{offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
          <Button size="sm" variant="secondary" disabled={offset + PAGE >= total}
            onClick={() => setOffset(offset + PAGE)}>Next</Button>
        </div>
      )}
    </section>
  )
}

function BrowseRow({ entry: e, onInstall }: { entry: PluginBrowseEntry; onInstall: () => void }) {
  const runs = runnableSummary(e.components)
  return (
    <li className="border border-edge rounded-lg p-3 min-w-0">
      <div className="flex items-start gap-3 min-w-0">
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2 min-w-0 flex-wrap">
            <span className="font-medium truncate min-w-0"><bdi>{clip(e.name, 120)}</bdi></span>
            {e.reviewed && <Badge tone="info">Reviewed</Badge>}
            {e.version && <span className="text-xs opacity-60">{clip(e.version, 40)}</span>}
          </div>
          <div className="text-xs opacity-60 truncate"><bdi>{clip(e.marketplace, 120)}</bdi></div>
          {e.description && <p className="text-sm opacity-70 truncate"><bdi>{clip(e.description, 500)}</bdi></p>}
          <p className="text-xs opacity-50 mt-0.5">
            {runs ?? CONTENTS_UNKNOWN}
          </p>
        </div>
        <div className="shrink-0">
          {e.installed
            ? <Badge tone="success">Installed</Badge>
            : <Button size="sm" onClick={onInstall} aria-label={`Install ${e.name}`}>Install</Button>}
        </div>
      </div>
    </li>
  )
}
