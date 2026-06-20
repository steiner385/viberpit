import { useCallback, useEffect, useRef } from 'react'
import { useState } from 'react'
import { Dashboard } from './components/Dashboard'
import { Terminal } from './components/Terminal'

// ── View state machine ───────────────────────────────────────────────────────
//
// 'dashboard' — shows the session list / account strip.
// 'session'   — full-screen terminal for a specific session.
//   `originElementId` — the id of the DOM element that triggered navigation
//   (used to restore focus when the user presses Back).

type View =
  | { type: 'dashboard' }
  | { type: 'session'; sessionId: string; originElementId?: string }

function App() {
  const [view, setView] = useState<View>({ type: 'dashboard' })
  const prevViewRef = useRef<View>({ type: 'dashboard' })

  // Track previous view so we can restore focus on back navigation
  useEffect(() => {
    prevViewRef.current = view
  })

  const handleOpenSession = useCallback((sessionId: string) => {
    // The session card button has id="row-<sessionId>" so focus can be restored
    setView({ type: 'session', sessionId, originElementId: `row-${sessionId}` })
  }, [])

  const handleBack = useCallback(() => {
    const prev = prevViewRef.current
    setView({ type: 'dashboard' })
    // Restore focus to the originating element after the re-render
    if (prev.type === 'session' && prev.originElementId) {
      const id = prev.originElementId
      // Use requestAnimationFrame to ensure the DOM is ready
      requestAnimationFrame(() => {
        document.getElementById(id)?.focus()
      })
    }
  }, [])

  return (
    <div className="min-h-screen bg-background text-foreground">
      {view.type === 'session' ? (
        <Terminal sessionId={view.sessionId} onBack={handleBack} />
      ) : (
        <>
          <header className="p-4 border-b border-border">
            <h1 className="text-2xl font-bold tracking-tight">ccswitch</h1>
          </header>
          <main className="p-4">
            <Dashboard onOpenSession={handleOpenSession} />
          </main>
        </>
      )}
    </div>
  )
}

export default App
