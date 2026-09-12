import { useMemo, useState } from 'react'
import { useConsolidation, useConsolidate } from '@/api/hooks'
import type { ActiveConsolidation, ConsolidationCandidate, ConsolidationCluster } from '@/api/automations'

type OutcomeLevel = 'ok' | 'warn' | 'error'

const OUTCOME_TONE: Record<OutcomeLevel, string> = {
  ok: 'text-ink',
  warn: 'text-warning',
  error: 'text-danger',
}

function ciDot(checks: string): string {
  if (checks === 'passing') return 'bg-success'
  if (checks === 'failing') return 'bg-danger'
  return 'bg-warning'
}

export function ConsolidatePanel({ enabled, onOpenSession }: {
  enabled: boolean
  onOpenSession?: (id: string) => void
}) {
  const { data } = useConsolidation(enabled)
  const consolidate = useConsolidate()
  const [selected, setSelected] = useState<Record<number, boolean>>({})
  const [outcome, setOutcome] = useState<{ level: OutcomeLevel; message: string } | null>(null)

  // v1: one repo group at a time. Pick the first repo with candidates; a repo
  // selector is a later enhancement. Selection is scoped to this repo so a
  // cross-repo consolidation can never be assembled.
  const [repo, group] = useMemo(() => {
    const repos = Object.entries(data?.repos ?? {})
    return repos.length ? repos[0] : ['', { candidates: [], clusters: [], active: [] }]
  }, [data]) as [string, {
    candidates: ConsolidationCandidate[]
    clusters: ConsolidationCluster[]
    active: ActiveConsolidation[]
  }]

  // PR number → live consolidation session covering it.
  const underway = useMemo(() => {
    const m = new Map<number, string>()
    for (const a of group.active ?? []) {
      for (const n of a.numbers) m.set(n, a.session_id)
    }
    return m
  }, [group])

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
    setOutcome(null)
    try {
      const r = await consolidate.mutateAsync({ repo, numbers: chosen })
      if (r.level === 'ok') setSelected({})
      setOutcome({ level: r.level, message: r.message })
    } catch {
      setOutcome({ level: 'error', message: 'Failed to consolidate' })
    }
  }

  if (!group.candidates.length) return null

  return (
    <div className="mt-3 border-t border-edge pt-3">
      <div className="mb-2 text-xs font-medium text-ink-muted">
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
                  ? 'bg-danger-wash text-danger hover:bg-danger-wash/90'
                  : 'bg-surface-raised text-ink hover:bg-surface-raised/90'
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
              <span className="rounded bg-danger-wash px-1 text-xs text-danger">conflict</span>
            )}
            {underway.has(c.number) && (
              <button
                type="button"
                aria-label={`consolidation underway for PR ${c.number} — open session`}
                onClick={() => onOpenSession?.(underway.get(c.number)!)}
                className="rounded bg-warning-wash px-1 text-xs text-warning hover:bg-warning-wash/90 shrink-0"
              >
                consolidating…
              </button>
            )}
          </li>
        ))}
      </ul>

      <button
        type="button"
        onClick={run}
        disabled={!canConsolidate}
        className="rounded bg-accent px-3 py-1 text-sm text-accent-ink disabled:opacity-40"
      >
        {consolidate.isPending ? 'Consolidating…' : `Consolidate ${chosen.length} PRs`}
      </button>
      {outcome && (
        <p
          role={outcome.level === 'error' ? 'alert' : 'status'}
          className={`mt-2 text-sm ${OUTCOME_TONE[outcome.level]}`}
        >
          {outcome.message}
        </p>
      )}
    </div>
  )
}
