// web/src/obs/logger.ts
/**
 * clientLogger — buffers frontend errors/warnings and forwards them to
 * POST /api/client-logs so they land in the same redacted, correlated JSON sink
 * as backend logs. Bounded ring buffer (drop-oldest); batched delivery on a
 * timer, on buffer pressure, and via sendBeacon on pagehide. Never throws.
 */
export type ClientLogLevel = 'error' | 'warn'

export interface ClientLogEntry {
  level: ClientLogLevel
  msg: string
  stack?: string
  url?: string
  userAgent?: string
  clientId?: string
  appVersion?: string
  requestId?: string
  sessionId?: string
}

interface LoggerOpts { flushMs?: number; maxBuffer?: number; endpoint?: string }

const CLIENT_ID = (() => {
  try { return crypto.randomUUID() } catch { return `c-${Date.now()}-${Math.floor(Math.random() * 1e6)}` }
})()
// Vite injects this at build; falls back to 'dev'.
const APP_VERSION = (import.meta as { env?: Record<string, string> }).env?.VITE_APP_VERSION ?? 'dev'

export class ClientLogger {
  private buf: ClientLogEntry[] = []
  private sessionId: string | null = null
  private readonly flushMs: number
  private readonly maxBuffer: number
  private readonly endpoint: string
  private timer: ReturnType<typeof setInterval> | null = null

  constructor(opts: LoggerOpts = {}) {
    this.flushMs = opts.flushMs ?? 5000
    this.maxBuffer = opts.maxBuffer ?? 200
    this.endpoint = opts.endpoint ?? '/api/client-logs'
  }

  get bufferSize(): number { return this.buf.length }

  setSessionId(id: string | null): void { this.sessionId = id }

  start(): void {
    if (this.timer !== null) return
    this.timer = setInterval(() => { void this.flush() }, this.flushMs)
  }

  error(msg: string, extra: Partial<ClientLogEntry> = {}): void { this.push('error', msg, extra) }
  warn(msg: string, extra: Partial<ClientLogEntry> = {}): void { this.push('warn', msg, extra) }

  private push(level: ClientLogLevel, msg: string, extra: Partial<ClientLogEntry>): void {
    const entry: ClientLogEntry = {
      level,
      msg: String(msg).slice(0, 4000),
      url: typeof location !== 'undefined' ? location.href : undefined,
      userAgent: typeof navigator !== 'undefined' ? navigator.userAgent : undefined,
      clientId: CLIENT_ID,
      appVersion: APP_VERSION,
      sessionId: this.sessionId ?? undefined,
      ...extra,
    }
    this.buf.push(entry)
    if (this.buf.length > this.maxBuffer) this.buf.splice(0, this.buf.length - this.maxBuffer)
  }

  async flush(): Promise<void> {
    if (this.buf.length === 0) return
    const entries = this.buf
    this.buf = []
    try {
      await fetch(this.endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entries }),
        keepalive: true,
      })
    } catch {
      // Never recurse into logging-about-logging; drop the batch.
    }
  }

  /** Best-effort last-gasp flush on pagehide/visibilitychange. */
  flushBeacon(): void {
    if (this.buf.length === 0) return
    const entries = this.buf
    this.buf = []
    try {
      navigator.sendBeacon?.(this.endpoint, new Blob([JSON.stringify({ entries })], { type: 'application/json' }))
    } catch {
      // ignore
    }
  }
}

export const clientLogger = new ClientLogger()
