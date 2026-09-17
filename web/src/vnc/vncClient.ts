// web/src/vnc/vncClient.ts
import RFB from '@novnc/novnc'
import { jitter, BACKOFF_BASE_MS, BACKOFF_MAX_MS } from '@/ws/client'

export type VncState = 'connecting' | 'connected' | 'reconnecting' | 'disabled' | 'nodisplay' | 'disconnected'

export function classifyClose(code: number): 'disabled' | 'nodisplay' | 'reconnect' {
  if (code === 4401) return 'disabled'
  if (code === 4404) return 'nodisplay'
  return 'reconnect'
}

export class VncClient {
  private rfb: InstanceType<typeof RFB> | null = null
  private listeners: Record<string, ((s: VncState) => void)[]> = {}
  private url: string
  private canvas: HTMLElement | null = null
  private reconnectAttempt = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private destroyed = false
  constructor(url: string) { this.url = url }
  on(ev: string, cb: (s: VncState) => void) { (this.listeners[ev] ||= []).push(cb) }
  private emit(ev: string, s: VncState) { (this.listeners[ev] || []).forEach(cb => cb(s)) }
  connect(canvas: HTMLElement) {
    this.canvas = canvas
    this.rfb = new RFB(canvas, this.url, {})
    this.rfb.scaleViewport = true            // mobile-first: fit to viewport
    this.rfb.addEventListener('connect', () => {
      this.reconnectAttempt = 0
      this.emit('connect', 'connected')
    })
    this.rfb.addEventListener('disconnect', (e) => {
      const kind = classifyClose(e?.detail?.clean ? (e.detail.code ?? 1000) : 1006)
      if (kind === 'disabled') this.emit('disconnect', 'disabled')
      else if (kind === 'nodisplay') this.emit('disconnect', 'nodisplay')
      else this.scheduleReconnect()
    })
  }
  /** Actually perform the "caller drives backoff reconnect" the disconnect handler
   *  promises: rebuild the RFB against the same canvas after a jittered exponential
   *  delay (matches SessionSocket's own reconnect in web/src/ws/client.ts). Without
   *  this, a transient close (idle x11vnc teardown, a network blip) parks the tab in
   *  'reconnecting' forever — the label was live but nothing ever retried. */
  private scheduleReconnect() {
    if (this.destroyed || this.reconnectTimer !== null) return
    this.emit('reconnecting', 'reconnecting')
    const delay = Math.min(BACKOFF_MAX_MS, jitter(BACKOFF_BASE_MS * Math.pow(2, this.reconnectAttempt)))
    this.reconnectAttempt++
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (this.destroyed || !this.canvas) return
      this.connect(this.canvas)
    }, delay)
  }
  /** Re-apply base fit so noVNC recomputes its scale against the current mount size.
   *  Toggling false→true forces the scaleViewport setter to recompute (it no-ops on
   *  an unchanged value). */
  refit() {
    if (!this.rfb) return
    this.rfb.scaleViewport = false
    this.rfb.scaleViewport = true
  }
  focus() { this.rfb?.focus() }
  blur() { this.rfb?.blur() }
  /** Inject a key into the live session. With `down` omitted, noVNC sends a
   *  press+release pair. No-ops when no RFB is connected (e.g. reconnecting). */
  sendKey(keysym: number, code: string | null, down?: boolean) {
    this.rfb?.sendKey(keysym, code, down)
  }
  disconnect() {
    this.destroyed = true
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.rfb?.disconnect()
    this.rfb = null
  }
}
