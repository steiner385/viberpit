/**
 * RICE, on the client — the read-out beside the four score fields.
 *
 * Mirrors `server/pm/score.py:rice_score`, which is the ONE formula the roadmap
 * actually orders by (it is what lands in the overlay and in the GitHub Projects
 * `Score` field). This is a preview of what the server will compute for the
 * numbers currently in the form, not a second opinion: keep the two in step, and
 * change them together.
 *
 * `null` means "not scored", which is what the server returns for a missing
 * input or a zero effort (the divisor). A zero in any of the other three is a
 * real score of zero — `RICE_MIN` allows it — so it must not be conflated with
 * unscored.
 */
export function riceScore(
  reach: number | null | undefined,
  impact: number | null | undefined,
  effort: number | null | undefined,
  confidence: number | null | undefined,
): number | null {
  if (reach == null || impact == null || effort == null || confidence == null || effort === 0) return null
  return Math.round((reach * impact * confidence / effort) * 100) / 100
}
