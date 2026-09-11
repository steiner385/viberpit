/**
 * Duration formatting shared by every CI-health surface. Originally lived in
 * the deleted `components/InsightsView.tsx`; this is the only copy.
 */
export function fmtSecs(s: number): string {
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}
