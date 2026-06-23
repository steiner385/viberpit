// web/src/vnc/vncClient.ts
import RFB from '@novnc/novnc'

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
  constructor(url: string) { this.url = url }
  on(ev: string, cb: (s: VncState) => void) { (this.listeners[ev] ||= []).push(cb) }
  private emit(ev: string, s: VncState) { (this.listeners[ev] || []).forEach(cb => cb(s)) }
  connect(canvas: HTMLElement) {
    this.rfb = new RFB(canvas, this.url, {})
    this.rfb.scaleViewport = true            // mobile-first: fit to viewport
    this.rfb.addEventListener('connect', () => this.emit('connect', 'connected'))
    this.rfb.addEventListener('disconnect', (e) => {
      const kind = classifyClose(e?.detail?.clean ? (e.detail.code ?? 1000) : 1006)
      if (kind === 'disabled') this.emit('disconnect', 'disabled')
      else if (kind === 'nodisplay') this.emit('disconnect', 'nodisplay')
      else this.emit('reconnecting', 'reconnecting')   // caller drives backoff reconnect
    })
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
  disconnect() { this.rfb?.disconnect(); this.rfb = null }
}
