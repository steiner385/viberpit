/**
 * Keep a wall display awake (shell spec §5): request a screen wake lock while
 * kiosk is on, re-request when the tab becomes visible again (the browser
 * releases the lock on hide), release on unmount.
 *
 * Best-effort by design: the API is missing in some browsers and refuses
 * without a user activation. A display that cannot hold the lock still works;
 * it just relies on the OS's own screen timeout, so every failure is swallowed.
 */
import { useEffect } from 'react'

interface WakeLockSentinel { release(): Promise<void> }
type WakeLockNavigator = Navigator & { wakeLock?: { request(type: 'screen'): Promise<WakeLockSentinel> } }

export function useWakeLock(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return
    const wakeLock = (navigator as WakeLockNavigator).wakeLock
    if (!wakeLock) return
    let sentinel: WakeLockSentinel | null = null
    let cancelled = false
    const request = () => {
      wakeLock.request('screen')
        .then((s) => { if (cancelled) void s.release().catch(() => {}); else sentinel = s })
        .catch(() => { /* unsupported, refused, or no activation — see above */ })
    }
    const onVisibility = () => { if (!document.hidden) request() }
    request()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', onVisibility)
      void sentinel?.release().catch(() => {})
      sentinel = null
    }
  }, [enabled])
}
