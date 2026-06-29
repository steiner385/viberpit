import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Wrench, SquareTerminal, ChevronRight, ChevronDown } from 'lucide-react'
import { GroupHeader } from './GroupHeader'
import { PrPill } from './PrPill'
import { StageTrack } from './StageTrack'
import { CheckDrillDown } from './CheckDrillDown'
import { useAllPrs, useFixPr, useStageModels } from '@/api/hooks'
import { isActionablePrState } from '@/session/prStatus'
import type { PipelinePr, StageModel } from '@/api/types'

export function PipelineBoard({
  prs,
  stageModels,
  onOpenSession,
  onFixPr,
}: {
  prs: PipelinePr[]
  stageModels: Record<string, StageModel>
  onOpenSession?: (sessionId: string) => void
  onFixPr?: (sessionId: string, number: number) => void
}) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const toggle = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }))
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const toggleExpand = (key: string) => setExpanded((e) => ({ ...e, [key]: !e[key] }))

  if (prs.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-8 text-center">
        No open PRs in your repos.
      </p>
    )
  }

  // Group PRs by repo.
  const byRepo: Record<string, PipelinePr[]> = {}
  for (const p of prs) {
    const repo = p.repo ?? ''
    if (!byRepo[repo]) byRepo[repo] = []
    byRepo[repo].push(p)
  }

  // Sort repos: failing-PR repos first, then alphabetically.
  const repos = Object.keys(byRepo).sort((a, b) => {
    const aFailing = byRepo[a].some((p) => p.checks === 'failing')
    const bFailing = byRepo[b].some((p) => p.checks === 'failing')
    if (aFailing !== bFailing) return bFailing ? 1 : -1
    return a.localeCompare(b)
  })

  return (
    <div className="space-y-3">
      {repos.map((repo) => {
        const rows = byRepo[repo]
        const model = stageModels[repo]
        return (
          <section key={repo}>
            <GroupHeader
              groupKey={`repo-${repo}`}
              label={repo}
              count={rows.length}
              collapsed={!!collapsed[repo]}
              onToggle={() => toggle(repo)}
            />
            {!collapsed[repo] && (
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
                        {p.check_groups && p.check_groups.length > 0 && (
                          <button
                            type="button"
                            data-testid={`pipeline-expand-${p.number}`}
                            onClick={(e) => {
                              e.stopPropagation()
                              toggleExpand(`${p.repo}#${p.number}`)
                            }}
                            aria-label={`Toggle checks for PR #${p.number}`}
                            className="shrink-0 text-muted-foreground"
                          >
                            {expanded[`${p.repo}#${p.number}`]
                              ? <ChevronDown size={14} aria-hidden />
                              : <ChevronRight size={14} aria-hidden />}
                          </button>
                        )}
                        <PrPill pr={p} />
                        <span className="truncate text-sm">{p.title}</span>
                        <StageTrack
                          model={model}
                          states={p.node_states}
                          etaSeconds={p.eta_seconds}
                          etaOverdue={p.eta_overdue}
                        />
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
                      {expanded[`${p.repo}#${p.number}`] && p.check_groups && (
                        <CheckDrillDown groups={p.check_groups} />
                      )}
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
  const { data: stageModels = {} } = useStageModels()
  const fixPr = useFixPr()
  return (
    <PipelineBoard
      prs={prs}
      stageModels={stageModels}
      onOpenSession={(id) => navigate(`/session/${id}`)}
      onFixPr={(id, number) => fixPr.mutate({ id, number })}
    />
  )
}
