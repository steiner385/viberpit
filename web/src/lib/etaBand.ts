export interface BandInput { elapsedS: number; lowS: number; nominalS: number; highS: number }
export interface BandSegments { solidPct: number; bandLeftPct: number; bandWidthPct: number; tickPct: number }

const clamp01 = (x: number) => Math.max(0, Math.min(1, x))

export function bandSegments(i: BandInput): BandSegments | null {
  const { elapsedS, lowS, nominalS, highS } = i
  for (const v of [elapsedS, lowS, nominalS, highS]) {
    if (v == null || Number.isNaN(v)) return null
  }
  const scale = elapsedS + highS
  if (scale <= 0) return null
  const bandLeft = clamp01((elapsedS + lowS) / scale)
  return {
    solidPct: clamp01(elapsedS / scale) * 100,
    bandLeftPct: bandLeft * 100,
    bandWidthPct: (1 - bandLeft) * 100,         // band spans low-completion → scale (= high completion)
    tickPct: clamp01((elapsedS + nominalS) / scale) * 100,
  }
}

// Node adapter: the PR aggregate has no single elapsed; derive one from percent + nominal.
export function pseudoElapsedFromPercent(nominalS: number, percent: number): number {
  return (nominalS * percent) / Math.max(100 - percent, 1)
}

// Live elapsed = the poll-time snapshot plus wall-clock since the browser received it.
// Delta floors at 0 (covers nowMs < updatedAtMs and the render-safe now=0 seed).
export function liveElapsedS(snapshotS: number, updatedAtMs: number, nowMs: number): number {
  return snapshotS + Math.max(0, (nowMs - updatedAtMs) / 1000)
}

// Overrun = the elapsed since the poll has consumed the entire p90 (high) remaining.
export function isOverrun(liveDeltaS: number, remHighS: number | null): boolean {
  return remHighS != null && liveDeltaS >= remHighS
}

export interface GanttRow { elapsed: number; expLow: number | null; expNominal: number | null; expHigh: number | null }
export interface GanttSegments { fillPct: number; bandLeftPct: number | null; bandWidthPct: number | null; tickPct: number | null }

// ccswitch stores REMAINING estimates; plotroom uses TOTAL expected. total = snapshot elapsed + remaining.
export function expectedTotals(row: {
  duration_seconds: number | null
  rem_low_seconds?: number | null
  rem_nominal_seconds?: number | null
  rem_high_seconds?: number | null
}): { expLow: number | null; expNominal: number | null; expHigh: number | null } {
  const base = row.duration_seconds ?? 0
  const add = (rem: number | null | undefined) => (rem == null ? null : base + rem)
  return { expLow: add(row.rem_low_seconds), expNominal: add(row.rem_nominal_seconds), expHigh: add(row.rem_high_seconds) }
}

// One shared time scale per panel: the longest elapsed-or-p90-expected across all rows (floor 60s).
export function ganttScale(rows: GanttRow[]): number {
  let max = 0
  for (const r of rows) max = Math.max(max, r.elapsed, r.expHigh ?? 0, r.expNominal ?? 0)
  return max > 0 ? max : 60
}

const pctOf = (v: number, scale: number) => Math.min(100, (v / scale) * 100)

export function ganttSegments(row: GanttRow, scale: number, queued = false): GanttSegments {
  const s = scale > 0 ? scale : 60
  const fillPct = queued ? 15 : pctOf(row.elapsed, s)
  let bandLeftPct: number | null = null
  let bandWidthPct: number | null = null
  if (row.expLow != null && row.expHigh != null) {
    bandLeftPct = pctOf(row.expLow, s)
    bandWidthPct = Math.max(0, pctOf(row.expHigh, s) - bandLeftPct)
  }
  const tickPct = row.expNominal != null ? pctOf(row.expNominal, s) : null
  return { fillPct, bandLeftPct, bandWidthPct, tickPct }
}
