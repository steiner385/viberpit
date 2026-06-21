import { Navigate, Route, Routes, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Dashboard, type TabId } from './components/Dashboard'
import { SessionView } from './components/session/SessionView'
import { ReviewView } from './components/ReviewView'

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
    <div className="min-h-screen bg-background text-foreground">
      <header className="p-4 border-b border-border">
        <h1 className="text-2xl font-bold tracking-tight">ccswitch</h1>
      </header>
      <main className="p-4">
        <Dashboard
          activeTab={tab}
          onTabChange={(next) => navigate(next === 'sessions' ? '/sessions' : `/${next}`)}
          onOpenSession={(id) => navigate(`/session/${id}`)}
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
  if (!sessionId) return <Navigate to="/sessions" replace />
  return (
    <div className="min-h-screen bg-background text-foreground">
      <SessionView sessionId={sessionId} onBack={() => navigate('/sessions')} />
    </div>
  )
}

function ReviewRoute() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  if (!sessionId) return <Navigate to="/sessions" replace />
  return (
    <div className="min-h-screen bg-background text-foreground">
      <ReviewView sessionId={sessionId} onBack={() => navigate('/sessions')} />
    </div>
  )
}

function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/sessions" replace />} />
      <Route path="/sessions" element={<DashboardRoute tab="sessions" />} />
      <Route path="/accounts" element={<DashboardRoute tab="accounts" />} />
      <Route path="/archived" element={<DashboardRoute tab="archived" />} />
      <Route path="/session/:sessionId" element={<SessionRoute />} />
      <Route path="/review/:sessionId" element={<ReviewRoute />} />
      <Route path="*" element={<Navigate to="/sessions" replace />} />
    </Routes>
  )
}

export default App
