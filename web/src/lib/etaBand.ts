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
