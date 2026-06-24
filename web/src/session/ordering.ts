import type { Session } from '@/api/types'
import { TIER_RANK, TIER_LABEL, type StatusTier } from './statusTier'
import { triageTier, triageRank } from './triageTier'
import { formatRepoLabel } from '@/lib/repoLabel'
import { isValidTimestamp } from '@/lib/relativeTime'

export type GroupBy = 'none' | 'repo' | 'status'
export type SortBy = 'status' | 'lastActive' | 'age' | 'name'

export interface SessionGroup {
  key: string
  label: string | null
  count: number
  sessions: Session[]
}

const byId = (a: Session, b: Session): number => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)

const lastActiveMs = (iso: string | null | undefined): number | null =>
  isValidTimestamp(iso) ? new Date(iso).getTime() : null

// Within-group comparators. Every non-status comparator ends with an id
// tie-break for stability across refetches. `status` intentionally omits it so
// it stays byte-identical to sortSessionsByTier (stable sort → input order).
const COMPARATORS: Record<SortBy, (a: Session, b: Session) => number> = {
  status: (a, b) => triageRank(a) - triageRank(b),
  lastActive: (a, b) => {
    const ta = lastActiveMs(a.updatedAt)
    const tb = lastActiveMs(b.updatedAt)
    if (ta === null && tb === null) return byId(a, b)
    if (ta === null) return 1
    if (tb === null) return -1
    return tb - ta || byId(a, b)
  },
  age: (a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime() || byId(a, b),
  name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) || byId(a, b),
}

export function orderSessions(
  sessions: readonly Session[],
  groupBy: GroupBy,
  sortBy: SortBy,
): SessionGroup[] {
  if (sessions.length === 0) return []
  const sorted = [...sessions].sort(COMPARATORS[sortBy])

  if (groupBy === 'none') {
    return [{ key: 'all', label: null, count: sorted.length, sessions: sorted }]
  }

  if (groupBy === 'status') {
    // Derive the tier list from TIER_RANK (single source of truth) so a newly-added
    // tier can't be silently dropped from the grouped view — adding one to the rank
    // table is enough. Ordered ascending by rank.
    const tiers = (Object.keys(TIER_RANK) as StatusTier[]).sort(
      (a, b) => TIER_RANK[a] - TIER_RANK[b],
    )
    const groups: SessionGroup[] = []
    for (const tier of tiers) {
      const inTier = sorted.filter((s) => triageTier(s) === tier)
      if (inTier.length) {
        groups.push({ key: tier, label: TIER_LABEL[tier], count: inTier.length, sessions: inTier })
      }
    }
    return groups
  }

  // groupBy === 'repo'
  const buckets = new Map<string, { label: string; isScratch: boolean; sessions: Session[] }>()
  for (const s of sorted) {
    const rl = formatRepoLabel(s.repo)
    const bucket = buckets.get(rl.text) ?? { label: rl.text, isScratch: rl.isScratch, sessions: [] }
    bucket.sessions.push(s)
    buckets.set(rl.text, bucket)
  }
  return [...buckets.values()]
    .sort(
      (a, b) =>
        (a.isScratch ? 1 : 0) - (b.isScratch ? 1 : 0) ||
        a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }),
    )
    .map((b) => ({ key: b.label, label: b.label, count: b.sessions.length, sessions: b.sessions }))
}
