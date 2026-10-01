import { useMemo, useState } from 'react'
import { AlertTriangle, Check, Clock, X } from 'lucide-react'
import { useConsolidation, useConsolidate } from '@/api/hooks'
import type { ActiveConsolidation, ConsolidationCandidate, ConsolidationCluster } from '@/api/automations'
import { Badge, type Tone } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { SectionHeading } from '@/ui/SectionHeading'
import { StatusPill } from '@/ui/StatusPill'
import { SelectableRow } from '@/ui/list/SelectableRow'

type OutcomeLevel = 'ok' | 'warn' | 'error'

const OUTCOME_TONE: Record<OutcomeLevel, string> = {
  ok: 'text-ink',
  warn: 'text-warning',
  error: 'text-danger',
}

// A candidate's checks as tone PLUS word (DESIGN.md's Tone-Plus-Word rule):
// the 8px colour-only dot this replaces was unnamed to a screen reader and
// meaningless to anyone who cannot tell its three colours apart
// (2026-10-01 Delivery critique, P1). Same icons `PrPill` uses for the same
// states, so a PR reads the same here and on a session card.
const CHECKS: Record<string, { label: string; Icon: typeof Check; tone: Tone }> = {
  passing: { label: 'passing', Icon: Check, tone: 'success' },
  failing: { label: 'failing', Icon: X, tone: 'danger' },
}
const CHECKS_PENDING = { label: 'pending', Icon: Clock, tone: 'warning' as Tone }

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
    <section className="mt-3 space-y-2 border-t border-edge pt-3">
      <SectionHeading>Consolidate PRs · {repo}</SectionHeading>

      {group.clusters.length > 0 && (
        // A cluster is a one-click selection, so it is a button of the system
        // — not a rounded-full chip of its own: "there is no selected/
        // unselected chip; selection in this system is a ring" (DESIGN.md).
        // A risky cluster says so with a glyph AND a word.
        <div className="flex flex-wrap gap-2">
          {group.clusters.map((cl) => (
            <Button
              key={cl.numbers.join('-')}
              type="button"
              size="sm"
              variant="secondary"
              onClick={() => selectCluster(cl)}
              iconLeft={cl.risk === 'risky' ? <AlertTriangle size={14} aria-hidden="true" className="text-danger" /> : undefined}
            >
              {cl.risk === 'risky' && <span className="sr-only">Risky: </span>}
              {cl.numbers.length} PRs · {cl.rationale}
            </Button>
          ))}
        </div>
      )}

      <ul className="space-y-1">
        {group.candidates.map((c) => {
          const checks = CHECKS[c.checks] ?? CHECKS_PENDING
          return (
            // The system's selection row (`ui/list/SelectableRow`, the sessions
            // list's) rather than a bare 13×13 native checkbox — one checkbox
            // kind on the page, labelled "Select PR N".
            <li key={c.number} className="min-h-target flex items-center text-sm">
              <SelectableRow
                id={String(c.number)}
                name={`PR ${c.number}`}
                checked={!!selected[c.number]}
                onToggle={(id) => toggle(Number(id))}
              >
                <span className="flex min-w-0 items-center gap-2">
                  <StatusPill label={checks.label} Icon={checks.Icon} tone={checks.tone} hideLabelNarrow className="shrink-0" />
                  <a href={c.url} target="_blank" rel="noreferrer" className="truncate hover:underline">
                    #{c.number} {c.title}
                  </a>
                  {c.mergeable === 'CONFLICTING' && <Badge tone="danger">conflict</Badge>}
                  {underway.has(c.number) && (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      className="shrink-0 text-warning"
                      aria-label={`consolidation underway for PR ${c.number} — open session`}
                      onClick={() => onOpenSession?.(underway.get(c.number)!)}
                    >
                      consolidating…
                    </Button>
                  )}
                </span>
              </SelectableRow>
            </li>
          )
        })}
      </ul>

      {/* Spawns a session: the `lg` primary button, like every heaviest
          action on a Queue tab (see `BacklogGroupsPanel`'s Dispatch). */}
      <Button
        type="button"
        size="lg"
        variant="primary"
        onClick={run}
        disabled={!canConsolidate}
        loading={consolidate.isPending}
      >
        {consolidate.isPending ? 'Consolidating…' : `Consolidate ${chosen.length} PRs`}
      </Button>
      {outcome && (
        <p
          role={outcome.level === 'error' ? 'alert' : 'status'}
          className={`text-sm ${OUTCOME_TONE[outcome.level]}`}
        >
          {outcome.message}
        </p>
      )}
    </section>
  )
}
