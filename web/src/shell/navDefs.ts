import {
  MessagesSquare, GitPullRequest, Workflow, Map as MapIcon,
  Settings,
  type LucideIcon,
} from 'lucide-react'
import { DELIVERY_PATH, deliveryScope } from '@/features/delivery/deliveryNavState'
import { DELIVERY, ROADMAP } from '@/features/products/vocab'

/** Which count `useNavBadges()` supplies for this section, if any. */
export type NavBadgeKey = 'sessions' | 'pipeline' | 'roadmap' | 'delivery' | 'settings'

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
   * One section qualifies today:
   * - `pipeline` — the only kiosk section before the AppShell cutover, and the
   *   only one whose action surface is gated (`StatusStrip` tiles render as
   *   plain divs; `PrRow` does not expand, which is where ready-merge lives).
   *
   * The old `insights` row was the second, and finish-line row 4 cost it the
   * flag: the check tables are still read-only, but they are now the CI health
   * SCOPE of `delivery`, whose other scope triggers automation jobs for real —
   * and `showInKiosk` gates a SECTION, not a scope. `kioskSafe` (below) is the
   * scope-level claim: `delivery` admits its CI scope through it, so a wall
   * display can be parked on CI health, though `?pages=` still cannot name it.
   *
   * The other four are `false` with the mutation that disqualifies each named
   * beside the row. Widening this is a per-section change with evidence, not a
   * default — see `navDefs.test.ts`'s pinned kiosk-safe set.
   */
  showInKiosk: boolean
  /**
   * Scope-level kiosk claim for a section that is NOT `showInKiosk`: admits the
   * read-only slice of it (verified by the same criterion) while the section as
   * a whole stays refused. Consulted only by `isKioskSafePath`, i.e. the route
   * guard — it does not make the section an attract-cycle `?pages=` member,
   * because that list names sections, not scopes.
   */
  kioskSafe?: (pathname: string, search: string) => boolean
  /** Renders a list column in `app.list`. Sessions, Roadmap and Delivery do
   *  today. */
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
 * Order and labels came over verbatim from the deleted `TAB_DEFS`, and are
 * pinned by equality in `navDefs.test.ts`. Two rows have changed since: Roadmap
 * Phase 3 replaced the `initiatives` and `projects` rows with a single
 * `roadmap` section, and finish-line row 4 replaced the `automations` and
 * `insights` rows with a single `delivery` section (finish-line spec §4.1). The
 * old paths of all four now redirect.
 *
 * `NavId` is derived from this array (below) rather than declared separately,
 * so there is nothing to keep in sync: adding a row adds a `NavId` member.
 */
const DEFS = [
  // showInKiosk:false — `MainPaneSession` renders `SessionActions`, whose Archive
  // is a direct `closeSession.mutate(sessionId)` with no confirm by design, and
  // `SessionBody`'s permission cards approve a pending tool call on one tap.
  { id: 'sessions',    label: 'Sessions',    icon: MessagesSquare,  path: '/sessions',    badge: 'sessions',    showInKiosk: false, hasList: true,
    help: 'Live sessions. Start one from the prompt; open one to read its transcript and reply. Switch the list to Archived to reread, restore or delete a past session.' },
  // showInKiosk:true — the pre-cutover kiosk section, and the one whose actions
  // consult `useInteractive()` (`StatusStrip` tiles, `PrRow` expansion).
  { id: 'pipeline',    label: 'Pipeline',    icon: GitPullRequest,  path: '/pipeline',    badge: 'pipeline',    showInKiosk: true,  hasList: false,
    help: 'Pull-request health for each repo, lane by lane, with the fix actions each lane offers.' },
  // showInKiosk:false — `RoadmapPage` advances initiatives (`advance.mutate`),
  // creates them, and syncs products; every one is a mutation. Since finish-line
  // row 3 folded Triage in as the Inbox scope it also carries the reason the flag
  // exists: that scope is keyboard-first — `a` accepts, `r` rejects and `A`
  // bulk-accepts the selection on ONE keystroke, with no confirm and a `window`
  // keydown listener, so a passer-by (or a stray keyboard on a wall display)
  // would empty the queue.
  //
  // `badge: 'roadmap'` is that queue's depth. It came off the deleted Triage row:
  // a queue nothing points at from outside the section is a queue nobody drains.
  { id: 'roadmap',     label: ROADMAP.label, icon: MapIcon,         path: '/roadmap',     badge: 'roadmap',     showInKiosk: false, hasList: true,
    help: ROADMAP.help },
  // showInKiosk:false — `AutomationControls` patches the automation (mode select,
  // enable switch) and `AutomationActions` runs
  // `trigger.mutateAsync({ item_id, kind })`, which fires the job for real. The
  // CI health scope is read-only (`CiScopeBody`, `CiRepoDetail` and `CheckTable`
  // render two `useQuery` reads into tables; the window `Select` is URL state),
  // so `kioskSafe` admits exactly that scope and nothing else.
  { id: 'delivery',    label: DELIVERY.label, icon: Workflow,       path: DELIVERY_PATH,  badge: 'delivery',    showInKiosk: false, hasList: true,
    kioskSafe: (pathname, search) => deliveryScope(pathname, search) === 'ci',
    help: DELIVERY.help },
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

/**
 * Paths that belong to a section without living under its path.
 *
 * Roadmap keeps `/initiatives/:slug` as the initiative detail URL (roadmap spec
 * §4) rather than folding it under `/roadmap`, so a prefix match alone resolves
 * it to NO section. Three things read `navDefForPath` and each degrades: the
 * rail highlights nothing, `hasList` is false so the Roadmap list column
 * vanishes from beside the page, and `OPEN_ITEM_RESOLVERS` is keyed by section
 * id — with no section, nothing reads `initiativeSlug` and the shell believes
 * nothing is open.
 *
 * It is NOT a kiosk gate. `KioskPageGuard` refuses anything whose def is not
 * `showInKiosk`, and a null def fails that check exactly as a `false` one does,
 * so the alias leaves kiosk behaviour unchanged in both directions.
 *
 * An alias is checked only AFTER every real path has failed, so it can never
 * outrank a section's own prefix.
 */
const SECTION_ALIASES: readonly { prefix: string; id: NavId }[] = [
  { prefix: '/initiatives', id: 'roadmap' },
]

/** The nav entry whose path prefixes `pathname` (longest match wins), else the
 *  one an alias claims it for, else null. */
export function navDefForPath(pathname: string): NavDef | null {
  let best: NavDef | null = null
  for (const d of NAV_DEFS) {
    if (pathname === d.path || pathname.startsWith(d.path + '/')) {
      if (!best || d.path.length > best.path.length) best = d
    }
  }
  if (best) return best
  for (const a of SECTION_ALIASES) {
    if (pathname === a.prefix || pathname.startsWith(a.prefix + '/')) return NAV_BY_ID[a.id]
  }
  return null
}

/**
 * Whether kiosk may be ON this location: its section is `showInKiosk`, or the
 * section's `kioskSafe` scope predicate admits it. The one reader of both claims
 * for the route guard and the tests that pin the refusal lists.
 */
export function isKioskSafePath(pathname: string, search: string): boolean {
  const def = navDefForPath(pathname)
  return !!def && (def.showInKiosk || (def.kioskSafe?.(pathname, search) ?? false))
}
