import {
  MessagesSquare, GitPullRequest, Workflow, Target,
  Package, Archive, LineChart, Settings,
  type LucideIcon,
} from 'lucide-react'

/** Which count `useNavBadges()` supplies for this section, if any. */
export type NavBadgeKey = 'sessions' | 'pipeline' | 'automations' | 'archived' | 'settings'

/** Shape of a nav entry, before `id` is narrowed to the derived union. */
interface NavDefShape {
  id: string
  label: string
  icon: LucideIcon
  /** Absolute route path; must match the route table in `routes.tsx`. */
  path: string
  badge?: NavBadgeKey
  /** Reachable as a kiosk page. Settings is not — kiosk is read-only. */
  showInKiosk: boolean
  /** Renders a list column in `app.list`. Only Sessions does today. */
  hasList: boolean
}

/**
 * The one app map. `NavRail`, `NavBar`, the route table, the kiosk page list and
 * the help sheet all derive from this array — adding a section means adding a
 * row here and nothing else.
 *
 * Replaces `components/tabDefs.ts` (`TAB_DEFS` + `buildTabs`); `TabId` moved
 * here from `Dashboard.tsx` and is renamed `NavId`.
 *
 * Order and labels are carried over verbatim from the deleted `TAB_DEFS`.
 * Note `projects` is labelled "Products" — that is intentional, not a typo.
 *
 * `NavId` is derived from this array (below) rather than declared separately,
 * so there is nothing to keep in sync: adding a row adds a `NavId` member.
 */
const DEFS = [
  { id: 'sessions',    label: 'Sessions',    icon: MessagesSquare,  path: '/sessions',    badge: 'sessions',    showInKiosk: true,  hasList: true  },
  { id: 'pipeline',    label: 'Pipeline',    icon: GitPullRequest,  path: '/pipeline',    badge: 'pipeline',    showInKiosk: true,  hasList: false },
  { id: 'automations', label: 'Automations', icon: Workflow,        path: '/automations', badge: 'automations', showInKiosk: true,  hasList: false },
  { id: 'initiatives', label: 'Initiatives', icon: Target,          path: '/initiatives',                       showInKiosk: true,  hasList: false },
  { id: 'projects',    label: 'Products',    icon: Package,         path: '/projects',                          showInKiosk: true,  hasList: false },
  { id: 'archived',    label: 'Archived',    icon: Archive,         path: '/archived',    badge: 'archived',    showInKiosk: true,  hasList: false },
  { id: 'insights',    label: 'Insights',    icon: LineChart,       path: '/insights',                          showInKiosk: true,  hasList: false },
  { id: 'settings',    label: 'Settings',    icon: Settings,        path: '/settings',    badge: 'settings',    showInKiosk: false, hasList: false },
] as const satisfies readonly NavDefShape[]

/** Derived from `DEFS` — the set of nav section ids. */
export type NavId = (typeof DEFS)[number]['id']

export interface NavDef extends NavDefShape {
  id: NavId
}

export const NAV_DEFS: readonly NavDef[] = DEFS

/** Indexes every entry by id. Throws at module load if two rows share an id. */
export const NAV_BY_ID: Record<NavId, NavDef> = (() => {
  const out = {} as Record<NavId, NavDef>
  for (const d of NAV_DEFS) {
    if (d.id in out) throw new Error(`navDefs: duplicate id ${d.id}`)
    out[d.id] = d
  }
  return out
})()

/** The nav entry whose path prefixes `pathname`, or null. Longest match wins. */
export function navDefForPath(pathname: string): NavDef | null {
  let best: NavDef | null = null
  for (const d of NAV_DEFS) {
    if (pathname === d.path || pathname.startsWith(d.path + '/')) {
      if (!best || d.path.length > best.path.length) best = d
    }
  }
  return best
}
