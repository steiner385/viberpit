import { useCallback, useEffect, useRef, useState } from 'react'
import { Dashboard } from './components/Dashboard'
import { SessionView } from './components/session/SessionView'
import { ReviewView } from './components/ReviewView'

// ── View state machine ───────────────────────────────────────────────────────
//
// 'dashboard' — shows the session list / account strip.
// 'session'   — full-screen terminal for a specific session.
// 'review'    — read-only transcript viewer for an archived session.
//   `originElementId` — the id of the DOM element that triggered navigation
//   (used to restore focus when the user presses Back).

type View =
  | { type: 'dashboard' }
  | { type: 'session'; sessionId: string; originElementId?: string }
  | { type: 'review'; sessionId: string }

function App() {
  const [view, setView] = useState<View>({ type: 'dashboard' })
  const prevViewRef = useRef<View>({ type: 'dashboard' })
  // Bumped on every back-navigation to re-focus the DispatchBar
  const [focusDispatchNonce, setFocusDispatchNonce] = useState(0)

  // Track previous view so we can restore focus on back navigation
  useEffect(() => {
    prevViewRef.current = view
  })

  const handleOpenSession = useCallback((sessionId: string) => {
    // The session card button has id="row-<sessionId>" so focus can be restored
    setView({ type: 'session', sessionId, originElementId: `row-${sessionId}` })
  }, [])

  const handleReviewSession = useCallback((sessionId: string) => {
    setView({ type: 'review', sessionId })
  }, [])

  const handleBack = useCallback(() => {
    setView({ type: 'dashboard' })
    // Bump the nonce to re-focus the DispatchBar after the dashboard mounts.
    // For keyboard-first users the prompt is always the right place to return to.
    setFocusDispatchNonce((n) => n + 1)
  }, [])

  const handleBackFromReview = useCallback(() => {
    // Return to dashboard with the archived tab active
    setView({ type: 'dashboard' })
    setFocusDispatchNonce((n) => n + 1)
  }, [])

  return (
    <div className="min-h-screen bg-background text-foreground">
      {view.type === 'session' ? (
        <SessionView sessionId={view.sessionId} onBack={handleBack} />
      ) : view.type === 'review' ? (
        <ReviewView sessionId={view.sessionId} onBack={handleBackFromReview} />
      ) : (
        <>
          <header className="p-4 border-b border-border">
            <h1 className="text-2xl font-bold tracking-tight">ccswitch</h1>
          </header>
          <main className="p-4">
            <Dashboard
              onOpenSession={handleOpenSession}
              onReviewSession={handleReviewSession}
              focusDispatchSignal={focusDispatchNonce}
            />
          </main>
        </>
      )}
    </div>
  )
}

export default App
