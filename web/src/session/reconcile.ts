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
 * TRUST_ORDER (FR-403) — which surface a session row in the list cache is taken from:
 *
 *   1. an OPEN WebSocket's patch (the cached row) when its rev token is newer than the
 *      poll's — a live frame that outran an in-flight poll;
 *   2. the polled DB shadow otherwise — a newer or equal token, or
 *   3. ANY poll while the socket is NOT open (closed, reconnecting, or judged stale): the
 *      cache may hold a frame the socket can no longer refresh, so the poll is the only
 *      surface still being fed and outranks it unconditionally.
 *
 * Deterministic by construction: the same (socketOpen, cachedRevAt, freshRevAt) triple
 * always resolves the same way. Returns true when the poll's row should replace the cache.
 */
export function pollOutranksCache(
  socketOpen: boolean,
  cachedRevAt: string | null | undefined,
  freshRevAt: string | null | undefined,
): boolean {
  if (!socketOpen) return true
  return !revIsNewer(cachedRevAt, freshRevAt)
}

export interface ReconcileOptions {
  /** Whether the route socket is currently `connected`. Defaults to true — the pre-FR-403
   *  behaviour, where a WS-newer cached row always survives the poll. */
  socketOpen?: boolean
}

/**
 * Merge a fresh poll result (`next`) against the current cache (`prev`) by TRUST_ORDER:
 * keep a cached session whose rev token is newer than the poll's while the socket is open
 * (a live WS patch that outran the in-flight poll); take the poll otherwise. `next` remains
 * authoritative for existence: a session absent from it is dropped. Intended as TanStack
 * Query `structuralSharing` for the sessions query.
 */
export function reconcileSessions(
  prev: Session[] | undefined,
  next: Session[],
  { socketOpen = true }: ReconcileOptions = {},
): Session[] {
  if (!prev) return next
  const prevById = new Map(prev.map((s) => [s.id, s]))
  return next.map((fresh) => {
    const cached = prevById.get(fresh.id)
    if (!cached) return fresh
    if (!pollOutranksCache(socketOpen, cached.revAt, fresh.revAt)) {
      // `unread` is per-viewer read state, not part of the record the rev token
      // versions (D-10): the poll's value is the fresher one even when the WS
      // patch won on the record.
      return fresh.unread === undefined || fresh.unread === cached.unread
        ? cached
        : { ...cached, unread: fresh.unread }
    }
    // A payload without the field must not erase it — absent reads as "read".
    return fresh.unread === undefined && cached.unread !== undefined
      ? { ...fresh, unread: cached.unread }
      : fresh
  })
}
