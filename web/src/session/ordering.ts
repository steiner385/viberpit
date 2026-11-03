import type { PullRequestInfo, Session } from '@/api/types'
import { TIER_RANK, TIER_LABEL } from './statusTier'
import { triageTier, triageRank } from './triageTier'
import { formatRepoLabel } from '@/lib/repoLabel'
import { isValidTimestamp } from '@/lib/relativeTime'

/**
 * The live list's Group-by values — the ONE list: `LIVE_SPEC`, the persisted
 * pref parser (`lib/sessionOrder`) and the controls' options all read it. The
 * values are persisted, so append only; never rename one.
 */
export const GROUP_BY_VALUES = ['none', 'repo', 'status', 'branch', 'host', 'date', 'pr'] as const
export type GroupBy = (typeof GROUP_BY_VALUES)[number]
export const SORT_BY_VALUES = ['status', 'lastActive', 'age', 'name'] as const
export type SortBy = (typeof SORT_BY_VALUES)[number]

export interface SessionGroup<T = Session> {
  key: string
  label: string | null
  count: number
  sessions: T[]
}

export interface GroupDef<T> {
  /** Map an item to its bucket. `order` sorts buckets (lower first); ties break on label.
   *  `now` is read once per ordering pass, so time-relative buckets (the date
   *  grouping) agree across every item and tests can pin the clock. */
  bucketOf: (item: T, now: Date) => { key: string; label: string; order?: number }
}

export interface OrderingConfig<T> {
  idOf: (item: T) => string
  /** Keyed by GroupBy value. 'none' is implicit (single null-label group). */
  groupings: Record<string, GroupDef<T>>
  /** Keyed by SortBy value. */
  sorts: Record<string, (a: T, b: T) => number>
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

/** Generic group+sort engine. Buckets via config.groupings[groupBy]; 'none' = flat. */
export function orderItems<T>(
  items: readonly T[],
  groupBy: string,
  sortBy: string,
  config: OrderingConfig<T>,
  now: Date = new Date(),
): SessionGroup<T>[] {
  if (items.length === 0) return []
  const sorted = [...items].sort(config.sorts[sortBy])
  const def = config.groupings[groupBy]
  if (groupBy === 'none' || !def) {
    return [{ key: 'all', label: null, count: sorted.length, sessions: sorted }]
  }
  const buckets = new Map<string, { label: string; order: number; sessions: T[] }>()
  for (const it of sorted) {
    const b = def.bucketOf(it, now)
    const cur = buckets.get(b.key) ?? { label: b.label, order: b.order ?? 0, sessions: [] }
    cur.sessions.push(it)
    buckets.set(b.key, cur)
  }
  return [...buckets.entries()]
    .sort(
      ([, a], [, b]) =>
        a.order - b.order || a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }),
    )
    .map(([key, b]) => ({ key, label: b.label, count: b.sessions.length, sessions: b.sessions }))
}

/** A grouping by an optional string field: one alphabetical group per value,
 *  then a trailing `missingLabel` group for null/blank. The missing key carries
 *  a colon, which neither a git ref nor a host name can, so it never collides. */
function byOptionalField(
  field: (s: Session) => string | null | undefined,
  missingKey: string,
  missingLabel: string,
): GroupDef<Session> {
  return {
    bucketOf: (s) => {
      const v = field(s)?.trim()
      return v ? { key: v, label: v, order: 0 } : { key: missingKey, label: missingLabel, order: 1 }
    },
  }
}

// Date buckets, by `updatedAt` in the viewer's local time. Bounds are local
// midnights built with the Date constructor (not `now - n * 86_400_000`), so a
// DST change cannot shift a bucket edge by an hour. A future timestamp (clock
// skew) is Today; a missing or invalid one is Older.
const DATE_BUCKETS = [
  { key: 'date:today', label: 'Today', daysBack: 0 },
  { key: 'date:yesterday', label: 'Yesterday', daysBack: 1 },
  { key: 'date:previous-7-days', label: 'Previous 7 days', daysBack: 7 },
] as const
const DATE_OLDER = { key: 'date:older', label: 'Older' } as const

function dateBucket(s: Session, now: Date): { key: string; label: string; order: number } {
  const t = lastActiveMs(s.updatedAt)
  if (t !== null) {
    for (const [order, b] of DATE_BUCKETS.entries()) {
      const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - b.daysBack)
      if (t >= start.getTime()) return { key: b.key, label: b.label, order }
    }
  }
  return { ...DATE_OLDER, order: DATE_BUCKETS.length }
}

// PR buckets, most advanced first: a session files under the lowest-ranked
// state among its PRs (open beats draft, merged beats closed).
const PR_BUCKETS: Record<PullRequestInfo['state'] | 'none', { label: string; order: number }> = {
  open: { label: 'Open', order: 0 },
  draft: { label: 'Draft', order: 1 },
  merged: { label: 'Merged', order: 2 },
  closed: { label: 'Closed', order: 3 },
  none: { label: 'No PR', order: 4 },
}

function prBucket(s: Session): { key: string; label: string; order: number } {
  let best: keyof typeof PR_BUCKETS = 'none'
  for (const pr of s.prs) {
    if (PR_BUCKETS[pr.state] && PR_BUCKETS[pr.state].order < PR_BUCKETS[best].order) best = pr.state
  }
  return { key: `pr:${best}`, ...PR_BUCKETS[best] }
}

/** Live-session ordering: repo/status/branch/host/date/PR grouping,
 *  status/lastActive/age/name sorts. */
export const LIVE_ORDERING: OrderingConfig<Session> = {
  idOf: (s) => s.id,
  groupings: {
    repo: {
      bucketOf: (s) => {
        const rl = formatRepoLabel(s.repo)
        return { key: rl.text, label: rl.text, order: rl.isScratch ? 1 : 0 }
      },
    },
    status: {
      bucketOf: (s) => {
        const tier = triageTier(s)
        return { key: tier, label: TIER_LABEL[tier], order: TIER_RANK[tier] }
      },
    },
    branch: byOptionalField((s) => s.branch, 'branch:none', 'No branch'),
    host: byOptionalField((s) => s.host, 'host:none', 'No host'),
    date: { bucketOf: dateBucket },
    pr: { bucketOf: prBucket },
  } satisfies Record<Exclude<GroupBy, 'none'>, GroupDef<Session>>,
  sorts: COMPARATORS,
}

/** Live wrapper — unchanged signature; existing callers/tests keep working. */
export function orderSessions(
  sessions: readonly Session[],
  groupBy: GroupBy,
  sortBy: SortBy,
  now: Date = new Date(),
): SessionGroup[] {
  return orderItems(sessions, groupBy, sortBy, LIVE_ORDERING, now)
}

export type NavItem<T = Session> =
  | { kind: 'header'; key: string; label: string; count: number; collapsed: boolean }
  | { kind: 'session'; session: T }

/**
 * Flatten ordered groups into the cursor's navigation domain: a labelled group
 * contributes a header item; a non-collapsed group also contributes its session
 * items. When a group has no label (groupBy='none') no header is emitted, so the
 * result is exactly the flat session list — the ungrouped view is unchanged.
 */
export function buildNavItems<T>(
  groups: readonly SessionGroup<T>[],
  collapsedKeys: ReadonlySet<string>,
): NavItem<T>[] {
  const items: NavItem<T>[] = []
  for (const g of groups) {
    const collapsed = collapsedKeys.has(g.key)
    if (g.label !== null) {
      items.push({ kind: 'header', key: g.key, label: g.label, count: g.count, collapsed })
    }
    if (!collapsed) {
      for (const s of g.sessions) items.push({ kind: 'session', session: s })
    }
  }
  return items
}
