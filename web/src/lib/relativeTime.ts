/**
 * Compact relative-time label: "5s ago", "2m ago", "3h ago", "3d ago".
 *
 * `now` is injectable so callers can render deterministically in tests; it
 * defaults to the wall clock. Future timestamps clamp to "0s ago" rather than
 * going negative (a session's updatedAt can briefly lead the client clock).
 */
/**
 * True when `iso` is a usable timestamp for {@link relTime}.
 *
 * Guards against the values the API can actually send for a session that has no
 * recorded activity: `null`, `undefined`, and `""`. These all coerce through
 * `new Date(...)` to **epoch 0** (a *finite* time), so a naive `isFinite` check
 * passes and the label renders "20627d ago". We additionally reject epoch 0
 * itself — no real session is active at 1970-01-01.
 */
export function isValidTimestamp(iso: string | null | undefined): iso is string {
  if (!iso) return false
  const t = new Date(iso).getTime()
  return Number.isFinite(t) && t > 0
}

export function relTime(iso: string, now: number = Date.now()): string {
  const secs = Math.max(0, (now - new Date(iso).getTime()) / 1000)
  if (secs < 60) return `${Math.round(secs)}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`
  return `${Math.round(secs / 86400)}d ago`
}
