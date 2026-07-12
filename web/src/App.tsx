import { Navigate, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Dashboard, type TabId } from './components/Dashboard'
import { DesktopShell } from './components/DesktopShell'
import { SessionView } from './components/session/SessionView'
import { SubSessionView } from './components/session/SubSessionView'
import { ReviewView } from './components/ReviewView'
import { GlobalUpdateBanner } from './components/GlobalUpdateBanner'
import { useMediaQuery } from './lib/useMediaQuery'
import { DeployControllerProvider } from './lib/deployController'
import { DeployOverlay } from './components/DeployOverlay'
import { useSessions, useArchivedSessions } from './api/hooks'
import { useSessionOrdering } from './session/useSessionOrdering'
import { nextVisibleSessionId } from './session/nextAfterArchive'

// Routing is the source of truth for which view is shown:
//   /sessions | /pipeline | /archived | /insights | /settings
//                                      → dashboard with that tab active
//   /settings?section=<general|accounts|automation|plugins|skills|commands|mcp>
//                                      → the Settings tab's active section
//   /accounts | /automation | /library → legacy top-level tabs; redirect to the
//                                        matching Settings section
//   /session/:sessionId                → full-screen terminal for a session
//   /review/:sessionId                 → read-only transcript for an archived session
// Dialogs are layered on the dashboard via a `?dialog=` search param (see Dashboard).

// Legacy /library?section=X deep links → /settings?section=X (section ids were
// preserved when the Library tab became Settings sections).
function LibraryRedirect() {
  const [params] = useSearchParams()
  const section = params.get('section') ?? 'plugins'
  return <Navigate to={`/settings?section=${section}`} replace />
}

function DashboardRoute({ tab }: { tab: TabId }) {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const rawDialog = searchParams.get('dialog')
  const dialog = rawDialog === 'new' ? 'new' : null
  // The settings modal became the Settings tab — honor old ?dialog=settings links.
  if (rawDialog === 'settings') return <Navigate to="/settings" replace />
  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-background text-foreground">
      {/* paddingTop pushes content below the iOS status bar; env() is 0 on desktop. */}
      <header className="shrink-0 px-4 pb-4 border-b border-border" style={{ paddingTop: 'calc(env(safe-area-inset-top) + 1rem)' }}>
        <h1 className="text-2xl font-bold tracking-tight">ccswitch</h1>
      </header>
      <main className="flex-1 min-h-0 flex flex-col px-4">
        <Dashboard
          activeTab={tab}
          onTabChange={(next) => navigate(next === 'sessions' ? '/sessions' : `/${next}`)}
          onOpenSession={(id, prompt) =>
            navigate(`/session/${id}`, prompt ? { state: { initialPrompt: prompt } } : undefined)
          }
          onReviewSession={(id) => navigate(`/review/${id}`)}
          dialog={dialog}
          onDialogChange={(next) => {
            setSearchParams((prev) => {
              const params = new URLSearchParams(prev)
              if (next) params.set('dialog', next)
              else params.delete('dialog')
              return params
            })
          }}
        />
      </main>
    </div>
  )
}

function SessionRoute() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const { data: sessions = [] } = useSessions()
  const { data: archived = [] } = useArchivedSessions()
  // Persisted sort/group order (no search filter — the search box isn't reachable
  // from inside a full-screen session). Same order the list showed.
  const { orderedSessions } = useSessionOrdering(sessions)
  // Carried via navigation state when a session is opened straight from create,
  // so the user's prompt shows immediately (seeded into the transcript). Absent
  // on a normal open / refresh / deep-link, in which case nothing is seeded.
  const initialPrompt = (location.state as { initialPrompt?: string } | null)?.initialPrompt
  if (!sessionId) return <Navigate to="/sessions" replace />
  // An archived session has no live driver, so the live-attach route dead-ends on
  // SESSION_NOT_FOUND. Redirect to the read-only review view (where the Archived
  // tab already sends archived sessions). Safety net for links that carry no status
  // — e.g. automation activity-log entries and stale bookmarks.
  if (archived.some((s) => s.id === sessionId)) {
    return <Navigate to={`/review/${sessionId}`} replace />
  }
  const handleDone = () => {
    const next = nextVisibleSessionId(orderedSessions.map((s) => s.id), sessionId)
    navigate(next ? `/session/${next}` : '/sessions')
  }
  return (
    <div className="flex-1 min-h-0 flex flex-col bg-background text-foreground">
      <SessionView
        key={sessionId}
        sessionId={sessionId}
        onBack={() => navigate('/sessions')}
        onDone={handleDone}
        onNavigateSession={(id) => navigate(`/session/${id}`)}
        initialPrompt={initialPrompt}
      />
    </div>
  )
}

function ReviewRoute() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  if (!sessionId) return <Navigate to="/sessions" replace />
  return (
    <div className="flex-1 min-h-0 flex flex-col bg-background text-foreground">
      <ReviewView sessionId={sessionId} onBack={() => navigate('/sessions')} />
    </div>
  )
}

function SubSessionRoute() {
  const { sessionId, agentId } = useParams()
  const navigate = useNavigate()
  if (!sessionId || !agentId) return <Navigate to="/sessions" replace />
  return (
    <div className="flex-1 min-h-0 flex flex-col bg-background text-foreground">
      <SubSessionView sessionId={sessionId} agentId={agentId} onBack={() => navigate(`/session/${sessionId}`)} />
    </div>
  )
}

function App() {
  // Wide screens get the master-detail DesktopShell; narrow screens keep the
  // routed full-screen views below. useMediaQuery returns false when matchMedia
  // is unavailable (jsdom), so tests default to the mobile/routed branch.
  const isWide = useMediaQuery('(min-width: 1024px)')
  return (
    <DeployControllerProvider>
      <GlobalUpdateBanner />
      {isWide ? (
        <DesktopShell />
      ) : (
        <Routes>
          <Route path="/" element={<Navigate to="/sessions" replace />} />
          <Route path="/sessions" element={<DashboardRoute tab="sessions" />} />
          <Route path="/archived" element={<DashboardRoute tab="archived" />} />
          <Route path="/pipeline" element={<DashboardRoute tab="pipeline" />} />
          <Route path="/insights" element={<DashboardRoute tab="insights" />} />
          <Route path="/settings" element={<DashboardRoute tab="settings" />} />
          {/* Legacy top-level tabs — now Settings sections. */}
          <Route path="/accounts" element={<Navigate to="/settings?section=accounts" replace />} />
          <Route path="/automation" element={<Navigate to="/settings?section=automation" replace />} />
          <Route path="/library" element={<LibraryRedirect />} />
          <Route path="/session/:sessionId" element={<SessionRoute />} />
          <Route path="/session/:sessionId/subagent/:agentId" element={<SubSessionRoute />} />
          <Route path="/review/:sessionId" element={<ReviewRoute />} />
          <Route path="*" element={<Navigate to="/sessions" replace />} />
        </Routes>
      )}
      <DeployOverlay />
    </DeployControllerProvider>
  )
}

export default App
