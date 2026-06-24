import type { Session } from '@/api/types'
import { TIER_RANK, TIER_LABEL } from './statusTier'
import { triageTier, triageRank } from './triageTier'
import { formatRepoLabel } from '@/lib/repoLabel'
import { isValidTimestamp } from '@/lib/relativeTime'

export type GroupBy = 'none' | 'repo' | 'status'
export type SortBy = 'status' | 'lastActive' | 'age' | 'name'

export interface SessionGroup<T = Session> {
  key: string
  label: string | null
  count: number
  sessions: T[]
}

export interface GroupDef<T> {
  /** Map an item to its bucket. `order` sorts buckets (lower first); ties break on label. */
  bucketOf: (item: T) => { key: string; label: string; order?: number }
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
): SessionGroup<T>[] {
  if (items.length === 0) return []
  const sorted = [...items].sort(config.sorts[sortBy])
  const def = config.groupings[groupBy]
  if (groupBy === 'none' || !def) {
    return [{ key: 'all', label: null, count: sorted.length, sessions: sorted }]
  }
  const buckets = new Map<string, { label: string; order: number; sessions: T[] }>()
  for (const it of sorted) {
    const b = def.bucketOf(it)
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

/** Live-session ordering: repo + status grouping, status/lastActive/age/name sorts. */
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
  },
  sorts: COMPARATORS,
}

/** Live wrapper — unchanged signature; existing callers/tests keep working. */
export function orderSessions(
  sessions: readonly Session[],
  groupBy: GroupBy,
  sortBy: SortBy,
): SessionGroup[] {
  return orderItems(sessions, groupBy, sortBy, LIVE_ORDERING)
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
