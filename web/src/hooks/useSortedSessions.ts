import { useMemo } from 'react'
import { useSessions } from '@/api/hooks'
import { sortByTier } from '@/session/statusTier'
import type { Session } from '@/api/types'

/**
 * useSessions() with the list ordered by status tier: needs-you + error first,
 * your-turn next, busy last — stable within a tier (preserves the REST order).
 *
 * Re-sorts only as the order actually changes. `sortByTier` is a stable sort and
 * the underlying REST order is itself stable poll-to-poll (DB order), so the
 * output order changes only when some session's tier flips (or a session is
 * added/removed) — a poll that changes only non-tier fields produces an
 * identically-ordered list, so cards never jitter under the user, yet the fresh
 * field values still flow through. Returns the same shape as useSessions() with
 * `data` replaced by the sorted array.
 */
export function useSortedSessions() {
  const query = useSessions()
  const data = query.data

  const sorted = useMemo<Session[] | undefined>(
    () => (data ? sortByTier(data, (s) => s.status) : undefined),
    [data],
  )

  return { ...query, data: sorted }
}
