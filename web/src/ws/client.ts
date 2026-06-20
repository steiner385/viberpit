/**
 * SessionSocket — single authenticated WebSocket to /ws that multiplexes terminal I/O
 * and control/event messages across all sessions (R7).
 *
 * Reconnect: on close/error, exponential backoff (500ms → 10s, jittered).
 * On reopen, re-sends `attach` for the active session so the server replays scrollback (SC-008).
 */

import type { QueryClient } from '@tanstack/react-query'
import type { Session } from '@/api/types'
import { QK } from '@/api/hooks'

// ── Inbound message shapes ───────────────────────────────────────────────────

export interface WsOutputMsg       { type: 'output';        sessionId: string; data: string }
export interface WsReplayCompleteMsg { type: 'replayComplete'; sessionId: string }
export interface WsStatusMsg       { type: 'status';        sessionId: string; status: string }
export interface WsUsageMsg        { type: 'usage';         account: string; session: { pct: number; resetsAt: string | null }; weekly: { pct: number; resetsAt: string | null } }
export interface WsRotatedMsg      { type: 'rotated';       sessionId: string; fromAccount: string; toAccount: string }
export interface WsSessionEndedMsg { type: 'sessionEnded';  sessionId: string; code: number }
export interface WsErrorMsg        { type: 'error';         sessionId?: string; code: string; message: string }

export type WsInboundMsg =
  | WsOutputMsg
  | WsReplayCompleteMsg
  | WsStatusMsg
  | WsUsageMsg
  | WsRotatedMsg
  | WsSessionEndedMsg
  | WsErrorMsg

// ── Callback registry type ───────────────────────────────────────────────────

type EventMap = {
  output:        (msg: WsOutputMsg) => void
  replayComplete:(msg: WsReplayCompleteMsg) => void
  status:        (msg: WsStatusMsg) => void
  usage:         (msg: WsUsageMsg) => void
  rotated:       (msg: WsRotatedMsg) => void
  sessionEnded:  (msg: WsSessionEndedMsg) => void
  error:         (msg: WsErrorMsg) => void
  connectionStateChange: (state: ConnectionState) => void
}

export type ConnectionState = 'connecting' | 'connected' | 'disconnected' | 'reconnecting'

// ── SessionSocket class ──────────────────────────────────────────────────────

const BACKOFF_BASE_MS = 500
const BACKOFF_MAX_MS  = 10_000

function jitter(ms: number): number {
  return ms * (0.8 + Math.random() * 0.4)
}

export class SessionSocket {
  private ws: WebSocket | null = null
  private _connectionState: ConnectionState = 'disconnected'
  private _activeSessionId: string | null = null
  private _reconnectAttempt = 0
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private _destroyed = false
  private _listeners: { [K in keyof EventMap]?: Set<EventMap[K]> } = {}
  private _queryClient: QueryClient | null = null
  private readonly url: string

  constructor(url: string = '/ws') {
    this.url = url
  }

  // ── Public state ────────────────────────────────────────────────────────────

  get connectionState(): ConnectionState { return this._connectionState }
  get activeSessionId(): string | null { return this._activeSessionId }

  setQueryClient(qc: QueryClient) {
    this._queryClient = qc
  }

  // ── Lifecycle ───────────────────────────────────────────────────────────────

  connect() {
    if (this._destroyed || this.ws?.readyState === WebSocket.OPEN) return
    this._setConnectionState(this._reconnectAttempt > 0 ? 'reconnecting' : 'connecting')
    const ws = new WebSocket(this.url)
    this.ws = ws

    ws.onopen = () => {
      this._reconnectAttempt = 0
      this._setConnectionState('connected')
      // Re-attach active session so server replays scrollback
      if (this._activeSessionId) {
        this._send({ type: 'attach', sessionId: this._activeSessionId })
      }
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data as string) as WsInboundMsg
        this._handleInbound(msg)
      } catch {
        // malformed frame — ignore
      }
    }

    ws.onerror = () => {
      // onclose will fire right after; let it drive reconnect
    }

    ws.onclose = () => {
      if (this._destroyed) return
      this._setConnectionState('disconnected')
      this._scheduleReconnect()
    }
  }

  destroy() {
    this._destroyed = true
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer)
      this._reconnectTimer = null
    }
    this.ws?.close()
    this.ws = null
  }

  // ── Outbound messages ───────────────────────────────────────────────────────

  attach(sessionId: string) {
    this._activeSessionId = sessionId
    this._send({ type: 'attach', sessionId })
  }

  detach(sessionId: string) {
    if (this._activeSessionId === sessionId) this._activeSessionId = null
    this._send({ type: 'detach', sessionId })
  }

  input(sessionId: string, data: string) {
    this._send({ type: 'input', sessionId, data })
  }

  resize(sessionId: string, cols: number, rows: number) {
    this._send({ type: 'resize', sessionId, cols, rows })
  }

  interrupt(sessionId: string) {
    this._send({ type: 'interrupt', sessionId })
  }

  // ── Event emitter ───────────────────────────────────────────────────────────

  on<K extends keyof EventMap>(event: K, cb: EventMap[K]) {
    if (!this._listeners[event]) {
      (this._listeners as Record<string, Set<unknown>>)[event] = new Set()
    }
    (this._listeners[event] as Set<EventMap[K]>).add(cb)
  }

  off<K extends keyof EventMap>(event: K, cb: EventMap[K]) {
    (this._listeners[event] as Set<EventMap[K]> | undefined)?.delete(cb)
  }

  // ── Private helpers ─────────────────────────────────────────────────────────

  private _send(msg: object) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg))
    }
  }

  private _setConnectionState(state: ConnectionState) {
    if (this._connectionState === state) return
    this._connectionState = state
    this._emit('connectionStateChange', state)
  }

  private _scheduleReconnect() {
    if (this._destroyed || this._reconnectTimer !== null) return
    const delay = Math.min(
      BACKOFF_MAX_MS,
      jitter(BACKOFF_BASE_MS * Math.pow(2, this._reconnectAttempt)),
    )
    this._reconnectAttempt++
    this._setConnectionState('reconnecting')
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null
      this.connect()
    }, delay)
  }

  private _emit<K extends keyof EventMap>(event: K, ...args: Parameters<EventMap[K]>) {
    const set = this._listeners[event] as Set<EventMap[K]> | undefined
    if (!set) return
    for (const cb of set) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ;(cb as (...a: unknown[]) => void)(...(args as unknown[]))
    }
  }

  private _handleInbound(msg: WsInboundMsg) {
    switch (msg.type) {
      case 'output':
        this._emit('output', msg)
        break
      case 'replayComplete':
        this._emit('replayComplete', msg)
        break
      case 'status':
        this._emit('status', msg)
        this._patchSessionStatus(msg.sessionId, msg.status as Session['status'])
        break
      case 'usage':
        this._emit('usage', msg)
        this._patchAccountUsage(msg)
        break
      case 'rotated':
        this._emit('rotated', msg)
        this._patchSessionAccount(msg.sessionId, msg.toAccount)
        break
      case 'sessionEnded':
        this._emit('sessionEnded', msg)
        this._patchSessionStatus(msg.sessionId, 'ended')
        break
      case 'error':
        this._emit('error', msg)
        break
    }
  }

  // ── TanStack Query cache patching ────────────────────────────────────────────

  private _patchSessionStatus(sessionId: string, status: Session['status']) {
    const qc = this._queryClient
    if (!qc) return
    qc.setQueryData<Session[]>(QK.sessions, (old) => {
      if (!old) return old
      return old.map((s) => s.id === sessionId ? { ...s, status, updatedAt: new Date().toISOString() } : s)
    })
  }

  private _patchSessionAccount(sessionId: string, accountName: string) {
    const qc = this._queryClient
    if (!qc) return
    qc.setQueryData<Session[]>(QK.sessions, (old) => {
      if (!old) return old
      return old.map((s) => s.id === sessionId ? { ...s, accountName, updatedAt: new Date().toISOString() } : s)
    })
  }

  private _patchAccountUsage(msg: WsUsageMsg) {
    const qc = this._queryClient
    if (!qc) return
    // Import Account type inline to avoid circular imports
    type AccountLike = { name: string; usage: { session: { pct: number; resetsAt: string | null }; weekly: { pct: number; resetsAt: string | null } } }
    qc.setQueryData<AccountLike[]>(QK.accounts, (old) => {
      if (!old) return old
      return old.map((a) =>
        a.name === msg.account
          ? { ...a, usage: { session: { pct: msg.session.pct, resetsAt: msg.session.resetsAt }, weekly: { pct: msg.weekly.pct, resetsAt: msg.weekly.resetsAt } } }
          : a,
      )
    })
  }
}

// ── Singleton ────────────────────────────────────────────────────────────────
// One socket per page — shared across all useSessionSocket callers.

let _singleton: SessionSocket | null = null

export function getSessionSocket(): SessionSocket {
  if (!_singleton) {
    _singleton = new SessionSocket('/ws')
    _singleton.connect()
  }
  return _singleton
}

// ── React hook ───────────────────────────────────────────────────────────────

import { useEffect, useState, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'

export function useSessionSocket() {
  const queryClient = useQueryClient()
  const socket = getSessionSocket()

  // Wire the query client once
  useEffect(() => {
    socket.setQueryClient(queryClient)
  }, [socket, queryClient])

  const [connectionState, setConnectionState] = useState<ConnectionState>(socket.connectionState)

  useEffect(() => {
    const handler = (state: ConnectionState) => setConnectionState(state)
    socket.on('connectionStateChange', handler)
    return () => socket.off('connectionStateChange', handler)
  }, [socket])

  const attach    = useCallback((sessionId: string) => socket.attach(sessionId), [socket])
  const detach    = useCallback((sessionId: string) => socket.detach(sessionId), [socket])
  const input     = useCallback((sessionId: string, data: string) => socket.input(sessionId, data), [socket])
  const resize    = useCallback((sessionId: string, cols: number, rows: number) => socket.resize(sessionId, cols, rows), [socket])
  const interrupt = useCallback((sessionId: string) => socket.interrupt(sessionId), [socket])

  const on  = useCallback(<K extends keyof EventMap>(event: K, cb: EventMap[K]) => socket.on(event, cb), [socket])
  const off = useCallback(<K extends keyof EventMap>(event: K, cb: EventMap[K]) => socket.off(event, cb), [socket])

  return { attach, detach, input, resize, interrupt, on, off, connectionState, socket }
}
