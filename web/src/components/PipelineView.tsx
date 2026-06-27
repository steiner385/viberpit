import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Wrench, SquareTerminal } from 'lucide-react'
import { GroupHeader } from './GroupHeader'
import { PrPill } from './PrPill'
import { useAllPrs, useFixPr } from '@/api/hooks'
import { isActionablePrState } from '@/session/prStatus'
import type { PipelinePr } from '@/api/types'

/** Format a positive seconds value as a human-readable ETA string: ~45s, ~12m, ~1h5m. */
export function formatEta(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  const rem = m % 60
  return rem === 0 ? `${h}h` : `${h}h${rem}m`
}

// Stage sections in urgency order; '' / unknown stages are dropped.
const STAGE_SECTIONS: { key: string; label: string }[] = [
  { key: 'parked', label: 'Parked' },
  { key: 'ci', label: 'In CI' },
  { key: 'ready', label: 'Ready' },
  { key: 'queue', label: 'Queue' },
  { key: 'merged', label: 'Merged' },
]

export function PipelineBoard({
  prs,
  onOpenSession,
  onFixPr,
}: {
  prs: PipelinePr[]
  onOpenSession?: (sessionId: string) => void
  onFixPr?: (sessionId: string, number: number) => void
}) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const toggle = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }))

  if (prs.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-8 text-center">
        No open PRs in your repos.
      </p>
    )
  }

  return (
    <div className="space-y-3">
      {STAGE_SECTIONS.map(({ key, label }) => {
        const rows = prs.filter((p) => (p.stage ?? '') === key)
        if (rows.length === 0) return null
        return (
          <section key={key}>
            <GroupHeader
              groupKey={`stage-${key}`}
              label={label}
              count={rows.length}
              collapsed={!!collapsed[key]}
              onToggle={() => toggle(key)}
            />
            {!collapsed[key] && (
              <ul className="mt-1 space-y-1">
                {rows.map((p) => {
                  const canFix =
                    !!p.session && p.checks === 'failing' && isActionablePrState(p.state)
                  return (
                    <li key={`${p.repo}#${p.number}`}>
                      <div
                        data-testid={`pipeline-row-${p.number}`}
                        role={p.session ? 'button' : undefined}
                        tabIndex={p.session ? 0 : undefined}
                        onClick={() => {
                          if (p.session) onOpenSession?.(p.session.id)
                          else window.open(p.url, '_blank', 'noopener,noreferrer')
                        }}
                        className="flex items-center gap-2 rounded px-2 py-1.5 hover:bg-muted/60 cursor-pointer"
                      >
                        <PrPill pr={p} />
                        <span className="text-xs text-muted-foreground tabular-nums">
                          {p.repo}
                        </span>
                        <span className="truncate text-sm">{p.title}</span>
                        {p.stage === 'ci' && (
                          p.eta_overdue
                            ? <span className="ml-1 shrink-0 text-xs text-destructive">overdue</span>
                            : p.eta_seconds != null
                              ? <span className="ml-1 shrink-0 text-xs text-muted-foreground">~{formatEta(p.eta_seconds)}</span>
                              : null
                        )}
                        {p.session && (
                          <span
                            data-testid={`pipeline-session-${p.number}`}
                            title={`Session: ${p.session.name}`}
                            className="ml-auto flex shrink-0 items-center gap-1 text-xs text-muted-foreground"
                          >
                            <SquareTerminal size={12} aria-hidden="true" className="flex-shrink-0" />
                            <span>{p.session.name}</span>
                          </span>
                        )}
                        {canFix && p.session && (
                          <button
                            type="button"
                            data-testid={`pipeline-fix-${p.number}`}
                            onClick={(e) => {
                              e.stopPropagation()
                              onFixPr?.(p.session!.id, p.number)
                            }}
                            aria-label={`Fix PR #${p.number}`}
                            className="ml-2 inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
                          >
                            <Wrench size={11} aria-hidden />
                            <span>Fix #{p.number}</span>
                          </button>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        )
      })}
    </div>
  )
}

export function PipelineView() {
  const navigate = useNavigate()
  const { data: prs = [] } = useAllPrs()
  const fixPr = useFixPr()
  return (
    <PipelineBoard
      prs={prs}
      onOpenSession={(id) => navigate(`/session/${id}`)}
      onFixPr={(id, number) => fixPr.mutate({ id, number })}
    />
  )
}
