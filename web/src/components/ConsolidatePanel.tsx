import { useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useConsolidation, useConsolidate } from '@/api/hooks'
import type { ConsolidationCandidate, ConsolidationCluster } from '@/api/automations'

function ciDot(checks: string): string {
  if (checks === 'passing') return 'bg-green-500'
  if (checks === 'failing') return 'bg-red-500'
  return 'bg-amber-500'
}

export function ConsolidatePanel({ enabled }: { enabled: boolean }) {
  const { data } = useConsolidation(enabled)
  const consolidate = useConsolidate()
  const [selected, setSelected] = useState<Record<number, boolean>>({})

  // v1: one repo group at a time. Pick the first repo with candidates; a repo
  // selector is a later enhancement. Selection is scoped to this repo so a
  // cross-repo consolidation can never be assembled.
  const [repo, group] = useMemo(() => {
    const repos = Object.entries(data?.repos ?? {})
    return repos.length ? repos[0] : ['', { candidates: [], clusters: [] }]
  }, [data]) as [string, { candidates: ConsolidationCandidate[]; clusters: ConsolidationCluster[] }]

  const chosen = group.candidates.filter((c) => selected[c.number]).map((c) => c.number)
  const canConsolidate = chosen.length >= 2 && !consolidate.isPending

  function toggle(n: number) {
    setSelected((s) => ({ ...s, [n]: !s[n] }))
  }

  function selectCluster(cluster: ConsolidationCluster) {
    setSelected((s) => {
      const next = { ...s }
      cluster.numbers.forEach((n) => { next[n] = true })
      return next
    })
  }

  async function run() {
    const r = await consolidate.mutateAsync({ repo, numbers: chosen })
    if (r.level === 'ok') { toast.success(r.message); setSelected({}) }
    else if (r.level === 'warn') toast.warning(r.message)
    else toast.error(r.message)
  }

  if (!group.candidates.length) return null

  return (
    <div className="mt-3 border-t border-neutral-800 pt-3">
      <div className="mb-2 text-xs font-medium text-neutral-400">
        Consolidate PRs · {repo}
      </div>

      {group.clusters.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {group.clusters.map((cl) => (
            <button
              key={cl.numbers.join('-')}
              type="button"
              onClick={() => selectCluster(cl)}
              className={`rounded-full px-3 py-1 text-xs ${
                cl.risk === 'risky'
                  ? 'bg-red-950 text-red-300 hover:bg-red-900'
                  : 'bg-neutral-800 text-neutral-200 hover:bg-neutral-700'
              }`}
            >
              {cl.numbers.length} PRs · {cl.rationale}
            </button>
          ))}
        </div>
      )}

      <ul className="mb-3 space-y-1">
        {group.candidates.map((c) => (
          <li key={c.number} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              aria-label={`select PR ${c.number}`}
              checked={!!selected[c.number]}
              onChange={() => toggle(c.number)}
            />
            <span className={`inline-block h-2 w-2 rounded-full ${ciDot(c.checks)}`} />
            <a href={c.url} target="_blank" rel="noreferrer" className="truncate hover:underline">
              #{c.number} {c.title}
            </a>
            {c.mergeable === 'CONFLICTING' && (
              <span className="rounded bg-red-950 px-1 text-xs text-red-300">conflict</span>
            )}
          </li>
        ))}
      </ul>

      <button
        type="button"
        onClick={run}
        disabled={!canConsolidate}
        className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-40"
      >
        {consolidate.isPending ? 'Consolidating…' : `Consolidate ${chosen.length} PRs`}
      </button>
    </div>
  )
}
