import type { Session } from '@/api/types'

/** Parse a rev token (ISO-8601 UTC string) to ms; NaN when absent/invalid. */
function revMs(revAt: string | null | undefined): number {
  return revAt ? Date.parse(revAt) : NaN
}

/** Strictly-newer-by-token. Absent candidate → false; absent existing but real candidate → true. */
export function revIsNewer(candidate: string | null | undefined, existing: string | null | undefined): boolean {
  const c = revMs(candidate)
  if (Number.isNaN(c)) return false
  const e = revMs(existing)
  if (Number.isNaN(e)) return true
  return c > e
}

/** Apply an incoming WS update unless it is strictly older than the cached one. A missing
 *  token on either side means there is no basis to reject → apply. */
export function shouldApplyRev(incoming: string | null | undefined, cached: string | null | undefined): boolean {
  const i = revMs(incoming), c = revMs(cached)
  if (Number.isNaN(i) || Number.isNaN(c)) return true
  return i >= c
}

/**
 * Merge a fresh poll result (`next`) against the current cache (`prev`), keeping a cached
 * session whose rev token is newer than the poll's — i.e. a live WS patch that outran the
 * in-flight poll. `next` remains authoritative for existence: a session absent from it is
 * dropped. Intended as TanStack Query `structuralSharing` for the sessions query.
 */
export function reconcileSessions(prev: Session[] | undefined, next: Session[]): Session[] {
  if (!prev) return next
  const prevById = new Map(prev.map((s) => [s.id, s]))
  return next.map((fresh) => {
    const cached = prevById.get(fresh.id)
    return cached && revIsNewer(cached.revAt, fresh.revAt) ? cached : fresh
  })
}
