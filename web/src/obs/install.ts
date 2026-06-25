// web/src/obs/install.ts
import { clientLogger, ClientLogger } from './logger'

/** Wire window.onerror + unhandledrejection to the client logger. Returns an uninstaller. */
export function installGlobalErrorHandlers(logger: ClientLogger = clientLogger): () => void {
  const onError = (e: ErrorEvent) => {
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
