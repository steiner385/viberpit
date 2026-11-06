// web/src/shell/RouteErrorFallback.tsx
/**
 * The route table's `errorElement` (#1418).
 *
 * React Router owns errors thrown while rendering a route — including a lazy
 * page's failed chunk import and a loader/action rejection — so the outer
 * `<ErrorBoundary>` in `main.tsx` never sees them, and without an
 * `errorElement` the router paints its built-in "Unexpected Application Error"
 * screen: a minified stack plus developer instructions. This replaces it.
 *
 * It is mounted on the pathless route BENEATH the layout, so the shell's
 * regions (nav, top bar) keep rendering and only the page slot shows the
 * fallback. A second instance sits above the layout to catch a crash in the
 * shell itself; there the nav is gone, so the fallback carries its own way out.
 *
 * Nothing about the error reaches the screen except a short correlation id. The
 * detail goes to `clientLogger` (the same redacted sink as every other client
 * error) under that id, so an operator can match the screenshot to the log.
 */
import { useEffect, useState } from 'react'
import { isRouteErrorResponse, useLocation, useRouteError } from 'react-router-dom'
import { Button } from '@/ui/Button'
import { clientLogger } from '@/obs/logger'
import { SESSIONS_PATH } from '@/session/sessionNavState'

/** A failed dynamic `import()` — a stale chunk after a deploy, or a dropped connection. */
function isChunkLoadError(error: unknown): boolean {
  const message = error instanceof Error ? `${error.name} ${error.message}` : String(error)
  return /ChunkLoadError|Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed/i
    .test(message)
}

function newCorrelationId(): string {
  try {
    return crypto.randomUUID().slice(0, 8)
  } catch {
    return Math.random().toString(16).slice(2, 10).padEnd(8, '0')
  }
}

function describe(error: unknown): { message: string; stack?: string } {
  if (isRouteErrorResponse(error)) return { message: `route response ${error.status} ${error.statusText}` }
  if (error instanceof Error) return { message: error.message, stack: error.stack }
  return { message: String(error) }
}

export function RouteErrorFallback() {
  const error = useRouteError()
  const { pathname } = useLocation()
  const [correlationId] = useState(newCorrelationId)
  const chunk = isChunkLoadError(error)

  useEffect(() => {
    const { message, stack } = describe(error)
    clientLogger.error(`route: [${correlationId}] ${message}`, { stack })
  }, [error, correlationId])

  return (
    <div
      role="alert"
      className="flex h-full min-h-64 flex-col items-center justify-center gap-3 p-6 text-center text-ink"
    >
      <p className="text-lg font-semibold">
        {chunk ? 'This page could not be loaded.' : 'This page hit a problem.'}
      </p>
      <p className="max-w-prose text-sm text-ink-muted">
        {chunk
          ? 'A newer version may have been deployed, or the connection dropped. Reloading fetches the current version.'
          : 'The error has been logged. Your sessions are not affected.'}
      </p>
      <p className="text-xs text-ink-muted">
        Reference <code data-testid="route-error-id">{correlationId}</code>
      </p>
      <div className="flex flex-wrap items-center justify-center gap-2">
        <Button type="button" variant="secondary" size="lg" onClick={() => window.location.reload()}>
          {chunk ? 'Reload' : 'Try again'}
        </Button>
        {pathname !== SESSIONS_PATH && (
          <Button type="button" variant="ghost" size="lg" onClick={() => window.location.assign(SESSIONS_PATH)}>
            Go to Sessions
          </Button>
        )}
      </div>
    </div>
  )
}
