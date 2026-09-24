// web/src/obs/install.ts
import { clientLogger, ClientLogger } from './logger'

/**
 * The browser's "ResizeObserver loop completed with undelivered notifications" (Chrome)
 * / "ResizeObserver loop limit exceeded" (older) is spec-defined: an observer callback
 * resized its own target, so delivery slipped a frame. Nothing is lost and it carries no
 * `error` object. Our layout observers (transcript spacer pin, QuestionGroup, toast
 * insets) trigger it routinely, and logging it at `error` filed it as an auto-error (#1993).
 */
function isBenignResizeObserverLoop(e: ErrorEvent): boolean {
  return !e.error && (e.message ?? '').startsWith('ResizeObserver loop')
}

/** Wire window.onerror + unhandledrejection to the client logger. Returns an uninstaller. */
export function installGlobalErrorHandlers(logger: ClientLogger = clientLogger): () => void {
  const onError = (e: ErrorEvent) => {
    if (isBenignResizeObserverLoop(e)) {
      logger.info(e.message)
      return
    }
    logger.error(e.message || 'window.error', {
      stack: e.error instanceof Error ? e.error.stack : undefined,
      url: e.filename,
    })
  }
  const onRejection = (e: PromiseRejectionEvent) => {
    const reason = e.reason
    logger.error(
      reason instanceof Error ? reason.message : `unhandledrejection: ${String(reason)}`,
      { stack: reason instanceof Error ? reason.stack : undefined },
    )
  }
  const onPageHide = () => logger.flushBeacon()
  const onVisibility = () => {
    if (document.visibilityState === 'hidden') logger.flushBeacon()
  }

  window.addEventListener('error', onError)
  window.addEventListener('unhandledrejection', onRejection)
  window.addEventListener('pagehide', onPageHide)
  document.addEventListener('visibilitychange', onVisibility)

  return () => {
    window.removeEventListener('error', onError)
    window.removeEventListener('unhandledrejection', onRejection)
    window.removeEventListener('pagehide', onPageHide)
    document.removeEventListener('visibilitychange', onVisibility)
  }
}
