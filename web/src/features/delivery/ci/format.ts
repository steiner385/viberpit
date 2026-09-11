/**
 * Duration formatting shared by every CI-health surface. Copied from
 * `components/InsightsView.tsx`, which keeps its own copy for now — Task 4
 * of this plan removes InsightsView's copy and has it import this one.
 */
export function fmtSecs(s: number): string {
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}
