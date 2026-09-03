import { Navigate, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useEffect, useRef } from 'react'
import { Dashboard, type TabId } from './components/Dashboard'
import { DesktopShell } from './components/DesktopShell'
import { SessionView } from './components/session/SessionView'
import { SubSessionView } from './components/session/SubSessionView'
import { ReviewView } from './components/ReviewView'
import { GlobalUpdateBanner } from './components/GlobalUpdateBanner'
import { DaemonRestartBanner } from './components/DaemonRestartBanner'
import { GitCredentialBanner } from './components/GitCredentialBanner'
import { BannerStack } from './ui/BannerStack'
import { BrandMark } from './components/BrandMark'
import { useMediaQuery } from './lib/useMediaQuery'
import { DeployControllerProvider } from './lib/deployController'
import { DeployOverlay } from './components/DeployOverlay'
import { useSessions, useArchivedSessions } from './api/hooks'
import { useSessionOrdering } from './session/useSessionOrdering'
import { nextSessionAfterClose } from './session/nextAfterArchive'
import { readInitialPrompt, sessionOpenState } from './session/sessionNavState'

// Routing is the source of truth for which view is shown:
//   /sessions | /pipeline | /automations | /archived | /insights | /settings
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
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-canvas text-ink">
      {/* paddingTop pushes content below the iOS status bar; env() is 0 on desktop. */}
      <header className="shrink-0 px-4 pb-4 border-b border-edge" style={{ paddingTop: 'calc(env(safe-area-inset-top) + 1rem)' }}>
        <div className="flex items-center gap-2.5">
          <BrandMark size={26} />
          <h1 className="text-2xl font-bold tracking-tight">ccswitch</h1>
        </div>
      </header>
      <main className="flex-1 min-h-0 flex flex-col px-4">
        <Dashboard
          activeTab={tab}
          onTabChange={(next) => navigate(next === 'sessions' ? '/sessions' : `/${next}`)}
          onOpenSession={(id, prompt) =>
            navigate(`/session/${id}`, sessionOpenState(prompt))
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
  const { data: sessions = [], isLoading: sessionsLoading } = useSessions()
  const { data: archived = [] } = useArchivedSessions()
  // Persisted sort/group order (no search filter — the search box isn't reachable
  // from inside a full-screen session). Same order the list showed.
  const { orderedSessions, allOrderedSessions } = useSessionOrdering(sessions)

  // Snapshot of the ordered IDs from the last render where the session was live.
  // The navigate-on-archive effect reads this to find the correct neighbor even
  // after orderedSessions has already dropped the session (reading refs in
  // effects is fine; we use a ref rather than state to avoid scheduling an
  // extra re-render on every session-list change).
  const lastOrderRef = useRef({
    visible: orderedSessions.map((s) => s.id),
    all: allOrderedSessions.map((s) => s.id),
  })
  useEffect(() => {
    if (sessionsLoading || !sessionId) return
    if (sessions.some((s) => s.id === sessionId)) {
      lastOrderRef.current = {
        visible: orderedSessions.map((s) => s.id),
        all: allOrderedSessions.map((s) => s.id),
      }
    }
  }, [sessions, sessionsLoading, sessionId, orderedSessions, allOrderedSessions])

  // Navigate away when the session appears in the archived list. Uses
  // lastOrderRef to distinguish two cases:
  //   • Was live (ref contains sessionId): the 503/409 error path — onSettled
  //     confirmed the archive after onError, so onDone was never called. Advance
  //     to the next live session using the pre-removal ordering snapshot.
  //   • Was never live (ref is empty for this id): stale bookmark — the URL was
  //     opened for an already-archived session (e.g. from an automation log link).
  //     Redirect to the read-only /review view instead.
  useEffect(() => {
    if (!sessionId || !archived.some((s) => s.id === sessionId)) return
    const wasLive =
      lastOrderRef.current.visible.includes(sessionId) ||
      lastOrderRef.current.all.includes(sessionId)
    if (!wasLive) {
      navigate(`/review/${sessionId}`, { replace: true })
      return
    }
    const next = nextSessionAfterClose(
      lastOrderRef.current.visible,
      lastOrderRef.current.all,
      sessionId,
    )
    navigate(next ? `/session/${next}` : '/sessions', { replace: true })
  }, [archived, sessionId, navigate])

  // Carried via navigation state when a session is opened straight from create,
  // so the user's prompt shows immediately (seeded into the transcript). Absent
  // on a normal open / refresh / deep-link, in which case nothing is seeded.
  // Both halves of that contract live in session/sessionNavState.
  const initialPrompt = readInitialPrompt(location.state)
  if (!sessionId) return <Navigate to="/sessions" replace />
  const handleDone = () => {
    // Fall back to the full ordering: the open session may be hidden from the
    // visible list (its group collapsed) and must still advance to a neighbor.
    const next = nextSessionAfterClose(
      orderedSessions.map((s) => s.id),
      allOrderedSessions.map((s) => s.id),
      sessionId,
    )
    navigate(next ? `/session/${next}` : '/sessions')
  }
  return (
    <div className="flex-1 min-h-0 flex flex-col bg-canvas text-ink">
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
    <div className="flex-1 min-h-0 flex flex-col bg-canvas text-ink">
      <ReviewView sessionId={sessionId} onBack={() => navigate('/sessions')} />
    </div>
  )
}

function SubSessionRoute() {
  const { sessionId, agentId } = useParams()
  const navigate = useNavigate()
  if (!sessionId || !agentId) return <Navigate to="/sessions" replace />
  return (
    <div className="flex-1 min-h-0 flex flex-col bg-canvas text-ink">
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
      <BannerStack className="px-4 pt-2" items={[
        { id: 'update', priority: 30, node: <GlobalUpdateBanner /> },
        { id: 'daemon', priority: 90, node: <DaemonRestartBanner /> },
        { id: 'git-credential', priority: 80, node: <GitCredentialBanner /> },
      ]} />
      {isWide ? (
        <DesktopShell />
      ) : (
        <Routes>
          <Route path="/" element={<Navigate to="/sessions" replace />} />
          <Route path="/sessions" element={<DashboardRoute tab="sessions" />} />
          <Route path="/archived" element={<DashboardRoute tab="archived" />} />
          <Route path="/pipeline" element={<DashboardRoute tab="pipeline" />} />
          <Route path="/automations" element={<DashboardRoute tab="automations" />} />
          <Route path="/initiatives" element={<DashboardRoute tab="initiatives" />} />
          <Route path="/projects" element={<DashboardRoute tab="projects" />} />
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
