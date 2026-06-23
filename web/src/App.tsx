import { Navigate, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Dashboard, type TabId } from './components/Dashboard'
import { DesktopShell } from './components/DesktopShell'
import { SessionView } from './components/session/SessionView'
import { SubSessionView } from './components/session/SubSessionView'
import { ReviewView } from './components/ReviewView'
import { GlobalUpdateBanner } from './components/GlobalUpdateBanner'
import { useMediaQuery } from './lib/useMediaQuery'

// Routing is the source of truth for which view is shown:
//   /sessions | /accounts | /archived  → dashboard with that tab active
//   /session/:sessionId                → full-screen terminal for a session
//   /review/:sessionId                 → read-only transcript for an archived session
// Dialogs are layered on the dashboard via a `?dialog=` search param (see Dashboard).

function DashboardRoute({ tab }: { tab: TabId }) {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const rawDialog = searchParams.get('dialog')
  const dialog = rawDialog === 'new' || rawDialog === 'settings' ? rawDialog : null
  return (
    <div className="min-h-dvh bg-background text-foreground">
      <header className="p-4 border-b border-border">
        <h1 className="text-2xl font-bold tracking-tight">ccswitch</h1>
      </header>
      <main className="p-4">
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
  // Carried via navigation state when a session is opened straight from create,
  // so the user's prompt shows immediately (seeded into the transcript). Absent
  // on a normal open / refresh / deep-link, in which case nothing is seeded.
  const initialPrompt = (location.state as { initialPrompt?: string } | null)?.initialPrompt
  if (!sessionId) return <Navigate to="/sessions" replace />
  return (
    <div className="min-h-dvh bg-background text-foreground">
      <SessionView sessionId={sessionId} onBack={() => navigate('/sessions')} initialPrompt={initialPrompt} />
    </div>
  )
}

function ReviewRoute() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  if (!sessionId) return <Navigate to="/sessions" replace />
  return (
    <div className="min-h-dvh bg-background text-foreground">
      <ReviewView sessionId={sessionId} onBack={() => navigate('/sessions')} />
    </div>
  )
}

function SubSessionRoute() {
  const { sessionId, agentId } = useParams()
  const navigate = useNavigate()
  if (!sessionId || !agentId) return <Navigate to="/sessions" replace />
  return (
    <div className="min-h-dvh bg-background text-foreground">
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
    <>
      <GlobalUpdateBanner />
      {isWide ? (
        <DesktopShell />
      ) : (
        <Routes>
          <Route path="/" element={<Navigate to="/sessions" replace />} />
          <Route path="/sessions" element={<DashboardRoute tab="sessions" />} />
          <Route path="/accounts" element={<DashboardRoute tab="accounts" />} />
          <Route path="/archived" element={<DashboardRoute tab="archived" />} />
          <Route path="/session/:sessionId" element={<SessionRoute />} />
          <Route path="/session/:sessionId/subagent/:agentId" element={<SubSessionRoute />} />
          <Route path="/review/:sessionId" element={<ReviewRoute />} />
          <Route path="*" element={<Navigate to="/sessions" replace />} />
        </Routes>
      )}
    </>
  )
}

export default App
