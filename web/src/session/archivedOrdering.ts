import type { ArchivedSession } from '@/api/types'
import { formatRepoLabel } from '@/lib/repoLabel'
import { repoFromCwd } from '@/lib/repoFromCwd'
import type { OrderingConfig } from './ordering'
import type { OrderingSpec } from './useSessionOrdering'

export type ArchGroupBy = 'none' | 'account' | 'repo'
export type ArchSortBy = 'archived' | 'created' | 'name'

const ms = (iso: string): number => {
  const t = new Date(iso).getTime()
  return Number.isNaN(t) ? 0 : t
}
const byId = (a: ArchivedSession, b: ArchivedSession): number =>
  a.id < b.id ? -1 : a.id > b.id ? 1 : 0

export const ARCHIVED_ORDERING: OrderingConfig<ArchivedSession> = {
  idOf: (s) => s.id,
  groupings: {
    account: {
      bucketOf: (s) => ({ key: s.account, label: s.account }),
    },
    repo: {
      bucketOf: (s) => {
        const rl = formatRepoLabel(repoFromCwd(s.cwd))
        return { key: rl.text, label: rl.text, order: rl.isScratch ? 1 : 0 }
      },
    },
  },
  sorts: {
    archived: (a, b) => ms(b.archivedAt) - ms(a.archivedAt) || byId(a, b),
    created: (a, b) => ms(b.createdAt) - ms(a.createdAt) || byId(a, b),
    name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) || byId(a, b),
  },
}

export const ARCHIVED_GROUP_OPTIONS: { value: ArchGroupBy; label: string }[] = [
  { value: 'none', label: 'None' },
  { value: 'account', label: 'Account' },
  { value: 'repo', label: 'Repo' },
]
export const ARCHIVED_SORT_OPTIONS: { value: ArchSortBy; label: string }[] = [
  { value: 'archived', label: 'Archived date' },
  { value: 'created', label: 'Created date' },
  { value: 'name', label: 'Name' },
]
export const ARCHIVED_DEFAULT_PREF: { groupBy: ArchGroupBy; sortBy: ArchSortBy } = {
  groupBy: 'none',
  sortBy: 'archived',
}

export const ARCHIVED_SPEC: OrderingSpec<ArchGroupBy, ArchSortBy, ArchivedSession> = {
  namespace: 'archived',
  config: ARCHIVED_ORDERING,
  groupByValues: ['none', 'account', 'repo'],
  sortByValues: ['archived', 'created', 'name'],
  defaultPref: ARCHIVED_DEFAULT_PREF,
}
