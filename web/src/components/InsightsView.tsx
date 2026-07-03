import { useMemo, useState } from 'react'
import { useStageModels, useCheckInsights } from '../api/hooks'
import { CheckTable, type Column } from './CheckTable'
import type { CheckDurationStat, CheckFlakeStat, CheckRegressionStat, RunnerWaitStat } from '../api/types'

const WINDOWS = ['24h', '7d', '14d', '30d']

function fmtSecs(s: number): string {
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}

export function InsightsView() {
  const { data: models } = useStageModels()
  const repos = useMemo(() => Object.keys(models ?? {}).sort(), [models])
  const [repo, setRepo] = useState<string | undefined>(undefined)
  const [timeWindow, setTimeWindow] = useState('7d')
  const selected = repo ?? repos[0]
  const { data } = useCheckInsights(selected, timeWindow)

  const slowCols: Column<CheckDurationStat>[] = [
    { header: 'Check', cell: (r) => r.check_name },
    { header: 'Event', cell: (r) => r.event },
    { header: 'p50', cell: (r) => fmtSecs(r.p50) },
    { header: 'p90', cell: (r) => fmtSecs(r.p90) },
    { header: 'Variability', cell: (r) => (r.ratio == null ? '—' : `${r.ratio.toFixed(1)}×`) },
    { header: 'n', cell: (r) => r.n },
  ]
  const flakeCols: Column<CheckFlakeStat>[] = [
    { header: 'Check', cell: (r) => r.check_name },
    { header: 'Event', cell: (r) => r.event },
    { header: 'Flake', cell: (r) => `${r.flake_pct.toFixed(1)}%` },
    { header: 'Flaky / total', cell: (r) => `${r.flaky_commits} / ${r.total_commits}` },
    { header: 'n', cell: (r) => r.n },
  ]

  return (
    <div className="space-y-3 p-2">
      <div className="flex items-center gap-3 text-sm">
        <label>
          Repo{' '}
          <select className="rounded border border-gray-300 dark:border-gray-700 bg-transparent px-1 py-0.5"
                  value={selected ?? ''} onChange={(e) => setRepo(e.target.value)}>
            {repos.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label>
          Window{' '}
          <select className="rounded border border-gray-300 dark:border-gray-700 bg-transparent px-1 py-0.5"
                  value={timeWindow} onChange={(e) => setTimeWindow(e.target.value)}>
            {WINDOWS.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
      </div>
      {!selected ? (
        <p className="text-xs text-gray-500">No repositories with pipeline history yet.</p>
      ) : (
        <>
          <CheckTable title="Slowest / most-variable checks" columns={slowCols}
            rows={data?.slowest ?? []} empty={`No check timing for ${selected} in this window.`} />
          <CheckTable title="Flakiest checks" columns={flakeCols}
            rows={data?.flakiest ?? []} empty={`No flaky checks for ${selected} in this window.`} />
          <CheckTable<CheckRegressionStat>
            title="Duration regressions (live — not affected by window)"
            rows={data?.regressions ?? []}
            empty="No regressions detected"
            columns={[
              { header: 'Check', cell: (r) => r.check_name },
              { header: 'Event', cell: (r) => r.event },
              { header: 'Was', cell: (r) => fmtSecs(r.prior_p50) },
              { header: 'Now', cell: (r) => fmtSecs(r.recent_p50) },
              { header: 'Slowdown', cell: (r) => `${r.ratio.toFixed(1)}×` },
              { header: 'Since', cell: (r) => r.since.slice(0, 10) },
            ]}
          />
          <CheckTable<RunnerWaitStat>
            title="Runner-wait health"
            rows={data?.runner_wait_health ?? []}
            empty="No runner-wait data"
            columns={[
              { header: 'Event', cell: (r) => r.event },
              { header: 'p50', cell: (r) => fmtSecs(r.p50) },
              { header: 'p90', cell: (r) => fmtSecs(r.p90) },
              {
                header: 'Δ vs prev',
                cell: (r) =>
                  r.prev_p50 == null
                    ? '—'
                    : `${r.p50 >= r.prev_p50 ? '↑' : '↓'} ${fmtSecs(Math.abs(r.p50 - r.prev_p50))}`,
              },
              { header: 'n', cell: (r) => String(r.n) },
            ]}
          />
        </>
      )}
    </div>
  )
}
