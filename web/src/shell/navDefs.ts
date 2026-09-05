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
  /**
   * Reachable as a kiosk page (`?kiosk=1&pages=…`, built in `shell/kiosk.ts`).
   *
   * **The criterion is "verified read-only", not "looks glanceable".** A kiosk
   * is an UNATTENDED wall display: nobody is standing in front of it, so every
   * control it renders is a control any passer-by can press. Set this `true`
   * only after READING the section's page component and every component it
   * renders, and confirming none of them can mutate server state — no
   * `useMutation`/`.mutate()`, no non-GET `api.*`, no control that reaches one.
   *
   * `shell/InteractionContext` is NOT that guarantee. It has four consumers
   * (`ui/Dialog`, `ui/PageHeader`, `pipeline/StatusStrip`, `pipeline/PrRow`);
   * an action button that never calls `useInteractive()` is live in kiosk, and
   * a destructive one that opens a `Dialog` is worse than live — kiosk refuses
   * the dialog, so the button is silently inert.
   *
   * Two sections qualify today:
   * - `pipeline` — the only kiosk section before the AppShell cutover, and the
   *   only one whose action surface is gated (`StatusStrip` tiles render as
   *   plain divs; `PrRow` does not expand, which is where ready-merge lives).
   * - `insights` — genuinely read-only: two `<select>`s over local view state
   *   feeding `useStageModels`/`useCheckInsights` (both `useQuery`) into
   *   `CheckTable`, which renders `<table>` and nothing else.
   *
   * The other five are `false` with the mutation that disqualifies each named
   * beside the row. Widening this is a per-section change with evidence, not a
   * default — see `navDefs.test.ts`'s pinned kiosk-safe set.
   */
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
  // showInKiosk:false — `MainPaneSession` renders `SessionActions`, whose Archive
  // is a direct `closeSession.mutate(sessionId)` with no confirm by design, and
  // `SessionBody`'s permission cards approve a pending tool call on one tap.
  { id: 'sessions',    label: 'Sessions',    icon: MessagesSquare,  path: '/sessions',    badge: 'sessions',    showInKiosk: false, hasList: true,
    help: 'Live sessions. Start one from the prompt; open one to read its transcript and reply.' },
  // showInKiosk:true — the pre-cutover kiosk section, and the one whose actions
  // consult `useInteractive()` (`StatusStrip` tiles, `PrRow` expansion).
  { id: 'pipeline',    label: 'Pipeline',    icon: GitPullRequest,  path: '/pipeline',    badge: 'pipeline',    showInKiosk: true,  hasList: false,
    help: 'Pull-request health for each repo, lane by lane, with the fix actions each lane offers.' },
  // showInKiosk:false — `AutomationsTab`'s action buttons run
  // `trigger.mutateAsync({ item_id, kind })`, which fires the job for real.
  { id: 'automations', label: 'Automations', icon: Workflow,        path: '/automations', badge: 'automations', showInKiosk: false, hasList: false,
    help: 'Scheduled and triggered jobs, their recent events, and the sessions they spawned.' },
  // showInKiosk:false — `InitiativesTab` has `advance.mutate(init.slug)` (moves an
  // initiative to the next SDLC stage) and `create.mutate(...)`.
  { id: 'initiatives', label: 'Initiatives', icon: Target,          path: '/initiatives',                       showInKiosk: false, hasList: false,
    help: 'The SDLC boards: every initiative as a stage track, with the artifact each gate needs.' },
  // showInKiosk:false — `ProjectsTab` has `create.mutate` and `sync.mutate(slug)`;
  // `TriageReviewList` has `accept.mutate`/`reject.mutate`; `OnboardingWizard` has
  // `proposeMap.mutate`/`classify.mutate`.
  { id: 'projects',    label: 'Products',    icon: Package,         path: '/projects',                          showInKiosk: false, hasList: false,
    help: 'The product registry: the repos ccswitch tracks, plus onboarding and triage review.' },
  // showInKiosk:false — `ArchivedSessionList`'s Unarchive is a direct
  // `unarchiveSession.mutate(id)`, and bulk unarchive/delete are one click from a
  // row checkbox. Its Permanent Delete opens a `Dialog`, which kiosk refuses —
  // so that button would be silently inert, which is its own confusion.
  { id: 'archived',    label: 'Archived',    icon: Archive,         path: '/archived',    badge: 'archived',    showInKiosk: false, hasList: false,
    help: 'Past sessions. Reread a transcript, unarchive it, or delete it permanently.' },
  // showInKiosk:true — verified read-only: `InsightsView` is two `<select>`s over
  // local state, two `useQuery` hooks, and `CheckTable`, which renders a table.
  { id: 'insights',    label: 'Insights',    icon: LineChart,       path: '/insights',                          showInKiosk: true,  hasList: false,
    help: 'Usage and account-rotation trends over time.' },
  // showInKiosk:false — accounts, rotation and preferences are all mutations.
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

/**
 * `/settings/<section>` — the path form of a Settings section (DEC-14).
 *
 * Lives here, beside the `/settings` it is built from, because three callers
 * need it: the route table's legacy redirects (`/accounts`, `/library`),
 * `SettingsIndexPage`'s `?section=` and last-section redirects, and
 * `SettingsNav`, which builds one link per row of `settingsDefs`.
 *
 * A blank section falls back rather than being passed through. `/settings?section=`
 * is a URL a stale link really can carry, and `/settings/ ` is a page that
 * exists, renders General, and shows a nonsense URL for the rest of the session.
 */
export function settingsSectionPath(section: string | null | undefined, fallback = 'general'): string {
  const trimmed = section?.trim()
  return `${NAV_BY_ID.settings.path}/${trimmed ? trimmed : fallback}`
}

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
