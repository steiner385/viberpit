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
