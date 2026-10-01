/**
 * The CI health detail — a verdict band in the pipeline's lane vocabulary over
 * the four check tables (slowest/most-variable, flakiest, duration
 * regressions, runner-wait health), moved onto tokens from the deleted
 * `components/InsightsView.tsx`. The repo comes from the route and the window
 * selector lives in the page header (`DeliveryPage`, via
 * `useSearchParamState('window', WINDOWS, '7d')`); this component only renders
 * the data for a given repo + window.
 */
import { Link } from 'react-router-dom'
import { useCheckInsights } from '@/api/hooks'
import type { Automation } from '@/api/automations'
import type { CheckDurationStat, CheckFlakeStat, CheckRegressionStat, RunnerWaitStat } from '@/api/types'
import { DELIVERY } from '@/features/products/vocab'
import { LANE_GLYPH, LANE_TONE, LANE_WORD } from '@/pipeline/spine/laneStatus'
import { Badge, TONE_TEXT } from '@/ui/Badge'
import { Banner } from '@/ui/Banner'
import { Skeleton } from '@/ui/Skeleton'
import { automationPath } from '../deliveryNavState'
import { CheckTable, type Column } from './CheckTable'
import { CICD_DOCTOR_ID, ciHealth, ciThresholds, flakeStatus } from './ciHealth'
import { fmtSecs } from './format'

// WINDOWS is consumed by the page header's useSearchParamState alongside this component;
// splitting it into its own file for one constant would be needless indirection.
// eslint-disable-next-line react-refresh/only-export-components
export const WINDOWS = ['24h', '7d', '14d', '30d'] as const
export type CiWindow = (typeof WINDOWS)[number]

export function CiRepoDetail({ repo, window, automations }: {
  repo: string
  window: CiWindow
  /** The automations list the page already holds: the CI/CD Doctor's row
   *  carries the flake thresholds every verdict here is judged by. */
  automations?: readonly Automation[]
}) {
  const { data, isLoading, isError } = useCheckInsights(repo, window)
  const thresholds = ciThresholds(automations)
  const health = ciHealth(data, thresholds)

  if (isError) {
    return <Banner tone="danger" title={DELIVERY.ciError} />
  }

  if (isLoading && !data) {
    return (
      <div role="status" aria-label="Loading CI health" className="space-y-3">
        {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} />)}
      </div>
    )
  }

  // Cells carry the verdict's tone beside the number (Tone-Plus-Word: the
  // number is the word). Before, every cell was 12px primary ink and the
  // reader had to know the thresholds (2026-10-01 Delivery critique, P2).
  const toned = (status: keyof typeof LANE_TONE, text: string) => (
    <span className={status === 'idle' || status === 'green' ? undefined : `font-medium ${TONE_TEXT[LANE_TONE[status]]}`}>{text}</span>
  )
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
    { header: 'Flake', cell: (r) => toned(flakeStatus(r.flake_pct, thresholds), `${r.flake_pct.toFixed(1)}%`) },
    { header: 'Flaky / total', cell: (r) => `${r.flaky_commits} / ${r.total_commits}` },
    { header: 'n', cell: (r) => r.n },
  ]

  return (
    <div className="space-y-3">
      {/* The verdict, in the same words the Pipeline page's health header
          uses for its lanes, and the way to the automation that acts on it. */}
      <div role="group" aria-label="CI health verdict" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <Badge dot tone={LANE_TONE[health.status]} />
        <span aria-hidden="true">{LANE_GLYPH[health.status]}</span>
        <span className={`font-medium ${TONE_TEXT[LANE_TONE[health.status]]}`}>{LANE_WORD[health.status]}</span>
        {health.reasons.length > 0 && <span className="text-ink-muted">{health.reasons.join(' · ')}</span>}
        {health.status === 'idle' && <span className="text-ink-muted">No check history in this window</span>}
        {thresholds && (
          <span className="text-xs text-ink-muted">watch ≥{thresholds.maxFlakePct}% · red ≥{thresholds.demoteFlakePct}% flake</span>
        )}
        <Link to={automationPath(CICD_DOCTOR_ID)} className="ml-auto text-accent hover:underline">Open CI/CD Doctor</Link>
      </div>
      <CheckTable title="Slowest / most-variable checks" columns={slowCols}
        rows={data?.slowest ?? []} empty={`No check timing for ${repo} in this window.`} />
      <CheckTable title="Flakiest checks" columns={flakeCols}
        rows={data?.flakiest ?? []} empty={`No flaky checks for ${repo} in this window.`} />
      <CheckTable<CheckRegressionStat>
        title="Duration regressions (live — not affected by window)"
        rows={data?.regressions ?? []}
        empty="No regressions detected"
        columns={[
          { header: 'Check', cell: (r) => r.check_name },
          { header: 'Event', cell: (r) => r.event },
          { header: 'Was', cell: (r) => fmtSecs(r.prior_p50) },
          { header: 'Now', cell: (r) => fmtSecs(r.recent_p50) },
          // Every listed regression is already past the server's ratio
          // (`history.py` `_REG_RATIO`), so each one reads as watch.
          { header: 'Slowdown', cell: (r) => toned('amber', `${r.ratio.toFixed(1)}×`) },
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
                : toned(r.p50 > r.prev_p50 ? 'amber' : 'green',
                    `${r.p50 >= r.prev_p50 ? '↑' : '↓'} ${fmtSecs(Math.abs(r.p50 - r.prev_p50))}`),
          },
          { header: 'n', cell: (r) => String(r.n) },
        ]}
      />
    </div>
  )
}
