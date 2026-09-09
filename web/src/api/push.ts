import { api } from '@/api/client'

export function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(b64)
  const out = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i)
  return out
}

export function pushSupported(): boolean {
  return typeof window !== 'undefined'
    && 'serviceWorker' in navigator
    && 'PushManager' in globalThis
    && 'Notification' in globalThis
}

export async function enablePush(): Promise<'subscribed' | 'denied' | 'unsupported'> {
  if (!pushSupported()) return 'unsupported'
  const cfg = await api.get<{ vapidPublicKey: string; enabled: boolean }>('/push/config')
  if (!cfg.enabled || !cfg.vapidPublicKey) return 'unsupported'
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') return 'denied'
  const reg = await navigator.serviceWorker.register('/sw.js')
  const existing = await reg.pushManager.getSubscription()
  const sub = existing ?? await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(cfg.vapidPublicKey) as unknown as ArrayBuffer,
  })
  await api.post('/push/subscribe', sub.toJSON())
  return 'subscribed'
}

export async function disablePush(): Promise<void> {
  if (!pushSupported()) return
  const reg = await navigator.serviceWorker.ready
  const sub = await reg.pushManager.getSubscription()
  if (sub) {
    await api.del('/push/subscribe?endpoint=' + encodeURIComponent(sub.endpoint))
    await sub.unsubscribe()
  }
}

/**
 * Whether this browsing context can receive Web Push as-is.
 *
 * `needs-install` is the case that matters: iOS Safari delivers Web Push only
 * to a web app added to the Home Screen, never to an ordinary tab. That is a
 * "not yet" — distinct from a browser that genuinely cannot.
 */
export type InstallState = 'installed' | 'needs-install' | 'not-required'

/** Running as an installed app: the standard media query, or Safari's
 *  pre-standard flag, which older iOS still reports. */
function isStandalone(): boolean {
  if (typeof window === 'undefined') return false
  if (window.matchMedia?.('(display-mode: standalone)').matches) return true
  return (window.navigator as Navigator & { standalone?: boolean }).standalone === true
}

/** iOS or iPadOS. iPadOS 13+ sends a desktop Safari UA and reports
 *  `platform === 'MacIntel'`, so touch points are the only reliable tell. */
function isIos(): boolean {
  if (typeof navigator === 'undefined') return false
  if (/iPad|iPhone|iPod/.test(navigator.userAgent)) return true
  return navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1
}

export function installState(): InstallState {
  if (isStandalone()) return 'installed'
  return isIos() ? 'needs-install' : 'not-required'
}
