/**
 * The route table — one table, every layout mode.
 *
 * ## Why this file exists (audit F2.1)
 *
 * A full sixteen-route table already existed, but only inside `App.tsx`'s
 * NARROW branch. At ≥1024px `App` rendered `DesktopShell`, which navigated with
 * `useState` (`tab`, `selection`, `reviewId`) and never touched the URL: a deep
 * link to `/pipeline` landed on Sessions, the Back button left the app, and a
 * link to a session could not be shared from a desktop browser. The table is
 * lifted out of that fork here so the ROUTER is the single source of navigation
 * truth in every layout mode, and the layout is just the element the routes
 * render into.
 *
 * ## Why a factory rather than a `ROUTES` constant
 *
 * `buildRoutes(layout)` takes the layout element instead of importing
 * `AppShell`. The shell is written in the task after this one, so importing it
 * here would not compile, and a placeholder import would be a lie that survives
 * into review. It also keeps the table testable on its own: the test mounts it
 * under a trivial layout and asserts URL behaviour without dragging in the
 * shell's regions.
 *
 * ## Legacy redirects are the compatibility layer
 *
 * Every session URL a user has bookmarked today is `/session/:id` (singular) and
 * every review URL is `/review/:id`. The canonical spellings are `/sessions/:id`
 * and `/sessions/archived/:archivedId`, so those old paths survive here as
 * redirects and nowhere else — `session/sessionNavState` is now the only thing
 * that builds a session path, so nothing in the app generates the legacy
 * spelling any more. The pre-scope-switch `/archived` and `/archived/:id`
 * spellings, from when Archived was its own nav section, redirect the same way.
 *
 * Every redirect REPLACES. A pushed redirect leaves the pre-redirect URL in the
 * history stack, so Back lands on it and is immediately bounced forward again —
 * the classic redirect loop, which the Back button can never escape.
 *
 * ## Pages are hosted unchanged — except where a page has since migrated
 *
 * The navigation PR moved navigation, not pages: each route rendered exactly
 * the component `DesktopShell`'s `mainPane` rendered, in the same container.
 * That is still true of every row below with one exception. Settings is now a
 * real page pair (`SettingsPage` / `SettingsIndexPage`, sub-spec C): the
 * fourteen-tab `SettingsView` strip is gone, `/settings/:section` is the whole
 * of DEC-14 rather than a prop handed to a component that also read
 * `?section=`, and bare `/settings` is a page whose element depends on the
 * layout mode. Every legacy Settings URL still resolves — `?section=` and
 * `?dialog=settings` are answered by `SettingsIndexPage` and
 * `SettingsDialogRedirect` respectively, `/accounts` and `/library` by the rows
 * below.
 */
/* eslint-disable react-refresh/only-export-components -- `buildRoutes` is a
   factory over the redirect components declared beside it. Splitting them apart
   would put the table and its own redirect targets in two files, which is the
   drift this file exists to prevent. */
import type { ReactElement } from 'react'
import {
  Navigate, Outlet, useLocation, useNavigate, useParams, useSearchParams, type RouteObject,
} from 'react-router-dom'
import { NAV_BY_ID, settingsSectionPath } from './navDefs'
import {
  SESSIONS_PATH, ARCHIVED_PATH, BACKGROUND_PATH,
  SESSION_ROUTE, SUBAGENT_ROUTE, ARCHIVED_SESSION_ROUTE,
  BACKGROUND_SESSION_ROUTE, BACKGROUND_SUBAGENT_ROUTE,
  LEGACY_ARCHIVED_PATH, LEGACY_ARCHIVED_SESSION_ROUTE,
  sessionPath, subAgentPath, archivedSessionPath, listScopeForPath,
} from '@/session/sessionNavState'
import { MainPaneSession } from '@/features/sessions/MainPaneSession'
import { WelcomePane } from '@/features/sessions/WelcomePane'
import { ArchivedWelcome } from '@/features/sessions/ArchivedWelcome'
import { BackgroundWelcome } from '@/features/sessions/BackgroundWelcome'
import { useArchiveAdvance } from '@/features/sessions/useArchiveAdvance'
import { SubSessionView } from '@/components/session/SubSessionView'
import { ReviewView } from '@/components/ReviewView'
import { PipelineTab } from '@/pipeline/PipelineTab'
import { AutomationsTab } from '@/automations/AutomationsTab'
import { TriageInbox } from '@/triage/TriageInbox'
import { InsightsView } from '@/components/InsightsView'
import { RoadmapPage } from '@/features/roadmap/RoadmapPage'
import { InitiativePage } from '@/features/roadmap/InitiativePage'
import {
  CAPABILITY_ROUTE, INITIATIVE_ROUTE, INITIATIVES_PATH, PRODUCT_ROUTE,
  productPath, roadmapPath,
} from '@/features/roadmap/roadmapNavState'
import { SettingsPage } from '@/features/settings/SettingsPage'
import { SettingsIndexPage } from '@/features/settings/SettingsIndexPage'

const SETTINGS_PATH = NAV_BY_ID.settings.path
const INSIGHTS_PATH = NAV_BY_ID.insights.path

// ── section pages (hosted unchanged; containers copied from DesktopShell) ─────

/**
 * Sessions: the open session, or the welcome pane when none is open.
 *
 * It renders `MainPaneSession` rather than `SessionView` because
 * `MainPaneSession` READS the session runtime from context while `SessionView`
 * CREATES one. `PageHost` already mounts `SessionRuntimeProvider` for exactly
 * this route (`useOpenSessionId` is non-null precisely when `:id` is present and
 * the section is `sessions`), so `SessionView` here would attach a second
 * runtime — two sockets, two transcripts — for every open session.
 *
 * `useArchiveAdvance` is `App.tsx`'s `SessionRoute` behaviour, ported here with
 * the routed shell it used to live in (Task 17): archiving the open session
 * advances to its neighbour rather than dead-ending on the welcome pane, a
 * session that leaves the live list without `onDone` firing (the 503/409 path)
 * advances anyway, and one that was never live at all — a bookmark of an
 * already-archived session — lands on its read-only transcript.
 */
/**
 * Serves BOTH `/sessions/:id` and `/sessions/background/:id`: a background
 * session is a live session the list keeps out of the way, so it gets the same
 * page and the same runtime. What differs is where its navigation lands — the
 * list scope it was opened from, read from the route exactly as the shell does.
 */
function SessionsPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const scope = listScopeForPath(useLocation().pathname)
  const onDone = useArchiveAdvance(id, scope)
  if (!id) return scope === 'background' ? <BackgroundWelcome /> : <WelcomePane />
  return (
    <MainPaneSession
      sessionId={id}
      onClosed={onDone}
      // Detaching (Back, ← / Esc / Ctrl+Z) goes to the list, not to the
      // neighbour: the session is still there, the user just left it.
      onDetach={() => navigate(scope === 'background' ? BACKGROUND_PATH : SESSIONS_PATH)}
      onOpenSession={(next) => navigate(sessionPath(next, scope))}
    />
  )
}

function SubAgentPage() {
  const { id, agentId } = useParams()
  const navigate = useNavigate()
  const scope = listScopeForPath(useLocation().pathname)
  if (!id || !agentId) return <Navigate to={SESSIONS_PATH} replace />
  return (
    <SubSessionView
      sessionId={id}
      agentId={agentId}
      onBack={() => navigate(sessionPath(id, scope))}
    />
  )
}

function PipelinePage() {
  const navigate = useNavigate()
  return (
    <div className="flex flex-col h-full">
      <PipelineTab onOpenInsights={() => navigate(INSIGHTS_PATH)} />
    </div>
  )
}

function AutomationsPage() {
  const navigate = useNavigate()
  return (
    <div className="overflow-y-auto h-full">
      <AutomationsTab
        onOpenSession={(id) => navigate(sessionPath(id))}
        onReviewSession={(id) => navigate(archivedSessionPath(id))}
      />
    </div>
  )
}

function TriagePage() {
  return <div className="flex flex-col h-full"><TriageInbox /></div>
}

function InsightsPage() {
  return <div className="p-4 overflow-y-auto h-full"><InsightsView /></div>
}

/** `/sessions/archived` — the archived scope's page slot. The archived LIST is
 *  the sessions column's archived scope (`ArchivedScopeBody`), not a page, so
 *  this is the empty state beside it. */
function ArchivedPage() {
  return (
    <div className="p-4 overflow-y-auto h-full">
      <ArchivedWelcome />
    </div>
  )
}

/** `/sessions/archived/:archivedId` — read-only transcript (DEC-6). Never gets
 *  a live runtime: the param is not `id`, so `useOpenSessionId` finds nothing. */
function ReviewPage() {
  const { archivedId } = useParams()
  const navigate = useNavigate()
  if (!archivedId) return <Navigate to={ARCHIVED_PATH} replace />
  return <ReviewView sessionId={archivedId} onBack={() => navigate(ARCHIVED_PATH)} />
}

// ── redirects ────────────────────────────────────────────────────────────────
//
// `<Navigate>` cannot interpolate params, so every parameterised redirect needs
// its own component. Each builds its target through `sessionNavState`, so a
// redirect cannot point somewhere the route table does not declare.

function RedirectSession() {
  const { sessionId } = useParams()
  return <Navigate to={sessionId ? sessionPath(sessionId) : SESSIONS_PATH} replace />
}

function RedirectSubAgent() {
  const { sessionId, agentId } = useParams()
  return (
    <Navigate
      to={sessionId && agentId ? subAgentPath(sessionId, agentId) : SESSIONS_PATH}
      replace
    />
  )
}

function RedirectReview() {
  const { sessionId } = useParams()
  return <Navigate to={sessionId ? archivedSessionPath(sessionId) : ARCHIVED_PATH} replace />
}

/** `/archived` → `/sessions/archived`. The search is carried so a reload of the
 *  redirected URL is still `?kiosk=1`. */
function RedirectLegacyArchived() {
  const { search } = useLocation()
  return <Navigate to={{ pathname: ARCHIVED_PATH, search }} replace />
}

/** `/archived/:sessionId` → `/sessions/archived/:sessionId`, search carried. */
function RedirectLegacyArchivedSession() {
  const { sessionId } = useParams()
  const { search } = useLocation()
  return (
    <Navigate
      to={{ pathname: sessionId ? archivedSessionPath(sessionId) : ARCHIVED_PATH, search }}
      replace
    />
  )
}

/** `/projects/:slug` → `/roadmap/:slug`. Products became the Roadmap section's
 *  top level in Phase 3; every bookmark of a product page lands on it. */
function RedirectProject() {
  const { slug } = useParams()
  return <Navigate to={slug ? productPath(slug) : NAV_BY_ID.roadmap.path} replace />
}

/**
 * Legacy `/library?section=X` deep links → `/settings/X`. Moved here verbatim
 * from `App.tsx` (only the target spelling changed): the library section ids
 * were preserved when the Library tab became Settings sections, including its
 * `plugins` default — `/library` with no section has always landed on Plugins,
 * and a bookmark of it must keep doing so.
 */
export function LibraryRedirect() {
  const [params] = useSearchParams()
  return <Navigate to={settingsSectionPath(params.get('section'), 'plugins')} replace />
}

/**
 * The legacy `?dialog=` search param.
 *
 * It is defined here, next to its only reader, rather than in a `dialogParam`
 * module. That module existed to serve `?dialog=new` — the reader, the name
 * union and the close helper for the New Session dialog — and went with it when
 * creating a session became the dock's job. What survives is this one string,
 * for one redirect off a URL shape the app stopped minting long ago. A module
 * whose whole remaining export is a constant for a legacy redirect is the sort
 * of half-dead file the next reader mistakes for live machinery.
 */
const DIALOG_PARAM = 'dialog'

/**
 * `?dialog=settings` on ANY path → `/settings/general`, param dropped.
 *
 * Settings used to be a modal layered over the dashboard, so old links carry it
 * as a search param rather than a path. It sits above the layout route so it
 * applies to every path including the catch-all, and so the shell is not mounted
 * only to be thrown away by the redirect.
 */
function SettingsDialogRedirect() {
  const [params] = useSearchParams()
  if (params.get(DIALOG_PARAM) === 'settings') return <Navigate to={settingsSectionPath(null)} replace />
  return <Outlet />
}

// ── the table ────────────────────────────────────────────────────────────────

const PAGES: RouteObject[] = [
  { path: '/', element: <Navigate to={SESSIONS_PATH} replace /> },

  { path: SESSIONS_PATH, element: <SessionsPage /> },
  // Before SESSION_ROUTE: `/sessions/archived` is a static sibling of
  // `/sessions/:id`, and the reader must not resolve as a session id.
  // (react-router ranks a static segment above a dynamic one regardless of
  // declaration order; this order documents that dependency.)
  { path: ARCHIVED_PATH, element: <ArchivedPage /> },
  { path: ARCHIVED_SESSION_ROUTE, element: <ReviewPage /> },
  // Same static-above-dynamic dependency for `/sessions/background`. Its detail
  // routes reuse the live pages on purpose: a background session IS live.
  { path: BACKGROUND_PATH, element: <SessionsPage /> },
  { path: BACKGROUND_SESSION_ROUTE, element: <SessionsPage /> },
  { path: BACKGROUND_SUBAGENT_ROUTE, element: <SubAgentPage /> },
  { path: SESSION_ROUTE, element: <SessionsPage /> },
  { path: SUBAGENT_ROUTE, element: <SubAgentPage /> },
  { path: NAV_BY_ID.pipeline.path, element: <PipelinePage /> },
  { path: NAV_BY_ID.automations.path, element: <AutomationsPage /> },
  // One page at three detail levels — portfolio, product, capability — chosen
  // from the params by `roadmapOpenItem`, never from the path.
  { path: NAV_BY_ID.roadmap.path, element: <RoadmapPage /> },
  { path: PRODUCT_ROUTE, element: <RoadmapPage /> },
  { path: CAPABILITY_ROUTE, element: <RoadmapPage /> },
  // Initiative detail keeps its own `/initiatives` prefix; `navDefForPath`'s
  // section alias is what puts it in the Roadmap section anyway.
  { path: INITIATIVE_ROUTE, element: <InitiativePage /> },
  { path: NAV_BY_ID.triage.path, element: <TriagePage /> },
  { path: INSIGHTS_PATH, element: <InsightsPage /> },
  { path: `${SETTINGS_PATH}/:section`, element: <SettingsPage /> },
  // Bare `/settings` is a PAGE, not a redirect: on phone and tablet-portrait it
  // is the grouped section list (the list half of list-then-detail). It still
  // redirects in the other modes, and it is where the legacy `?section=` deep
  // link is resolved — see `SettingsIndexPage`.
  { path: SETTINGS_PATH, element: <SettingsIndexPage /> },

  // Legacy paths — bookmarks only. Nothing in the app links here any more.
  { path: '/session/:sessionId', element: <RedirectSession /> },
  { path: '/session/:sessionId/subagent/:agentId', element: <RedirectSubAgent /> },
  { path: '/review/:sessionId', element: <RedirectReview /> },
  { path: LEGACY_ARCHIVED_PATH, element: <RedirectLegacyArchived /> },
  { path: LEGACY_ARCHIVED_SESSION_ROUTE, element: <RedirectLegacyArchivedSession /> },
  { path: '/accounts', element: <Navigate to={settingsSectionPath('accounts')} replace /> },
  { path: '/automation', element: <Navigate to={settingsSectionPath('automation')} replace /> },
  { path: '/library', element: <LibraryRedirect /> },
  // Roadmap Phase 3 folded the Products and Initiatives sections into `/roadmap`.
  // The Initiatives tab becomes the list column's Initiatives SCOPE, so its
  // bookmark lands there rather than on the Map.
  { path: '/projects', element: <Navigate to={NAV_BY_ID.roadmap.path} replace /> },
  { path: '/projects/:slug', element: <RedirectProject /> },
  { path: INITIATIVES_PATH, element: <Navigate to={roadmapPath('initiatives')} replace /> },

  { path: '*', element: <Navigate to={SESSIONS_PATH} replace /> },
]

/**
 * The whole table, rendered into `layout` — one layout route with every page as
 * a child, so the shell does not unmount on navigation.
 *
 * @param layout the element every page renders inside (`<AppShell />` in the app,
 *   a stand-in in tests). It must render an `<Outlet />`.
 */
export function buildRoutes(layout: ReactElement): RouteObject[] {
  return [
    {
      element: <SettingsDialogRedirect />,
      children: [{ element: layout, children: PAGES }],
    },
  ]
}
