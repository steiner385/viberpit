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
  /**
   * One line of help, rendered in the Navigation list of `HelpSheet`.
   *
   * Required, not optional, and that is the point: `HelpDrawer`'s hand-written
   * `NAV_MAP` described five sections while the app had eight (audit F1.3),
   * because nothing forced the two lists to move together. Here a new section is
   * a new row, and a row without help text does not compile.
   */
  help: string
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
  { id: 'sessions',    label: 'Sessions',    icon: MessagesSquare,  path: '/sessions',    badge: 'sessions',    showInKiosk: true,  hasList: true,
    help: 'Live sessions. Start one from the prompt; open one to read its transcript and reply.' },
  { id: 'pipeline',    label: 'Pipeline',    icon: GitPullRequest,  path: '/pipeline',    badge: 'pipeline',    showInKiosk: true,  hasList: false,
    help: 'Pull-request health for each repo, lane by lane, with the fix actions each lane offers.' },
  { id: 'automations', label: 'Automations', icon: Workflow,        path: '/automations', badge: 'automations', showInKiosk: true,  hasList: false,
    help: 'Scheduled and triggered jobs, their recent events, and the sessions they spawned.' },
  { id: 'initiatives', label: 'Initiatives', icon: Target,          path: '/initiatives',                       showInKiosk: true,  hasList: false,
    help: 'The SDLC boards: every initiative as a stage track, with the artifact each gate needs.' },
  { id: 'projects',    label: 'Products',    icon: Package,         path: '/projects',                          showInKiosk: true,  hasList: false,
    help: 'The product registry: the repos ccswitch tracks, plus onboarding and triage review.' },
  { id: 'archived',    label: 'Archived',    icon: Archive,         path: '/archived',    badge: 'archived',    showInKiosk: true,  hasList: false,
    help: 'Past sessions. Reread a transcript, unarchive it, or delete it permanently.' },
  { id: 'insights',    label: 'Insights',    icon: LineChart,       path: '/insights',                          showInKiosk: true,  hasList: false,
    help: 'Usage and account-rotation trends over time.' },
  { id: 'settings',    label: 'Settings',    icon: Settings,        path: '/settings',    badge: 'settings',    showInKiosk: false, hasList: false,
    help: 'Accounts, rotation and preferences. Each account is an isolated CLAUDE_CONFIG_DIR.' },
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
