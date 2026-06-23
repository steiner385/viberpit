/**
 * SessionSocket — single authenticated WebSocket to /ws that multiplexes terminal I/O
 * and control/event messages across all sessions (R7).
 *
 * Reconnect: on close/error, exponential backoff (500ms → 10s, jittered).
 * On reopen, re-sends `attach` for the active session so the server replays session history (SC-008).
 */

import type { QueryClient } from '@tanstack/react-query'
import type { Session } from '@/api/types'
import { QK } from '@/api/hooks'
import type { SessionEvent } from '@/session/events'
import { SESSION_EVENT_TYPES } from '@/session/events'

// ── Inbound message shapes ───────────────────────────────────────────────────

export interface WsStatusMsg       { type: 'status';        sessionId: string; status: string }
export interface WsUsageMsg        { type: 'usage';         account: string; session: { pct: number; resetsAt: string | null }; weekly: { pct: number; resetsAt: string | null } }
export interface WsRotatedMsg      { type: 'rotated';       sessionId: string; fromAccount: string; toAccount: string; reason?: string; turnRestarted?: boolean }
export interface WsErrorMsg        { type: 'error';         sessionId?: string; code: string; message: string; resetsAt?: string | null }
export interface WsBatchMsg        { type: 'batch';         events: SessionEvent[] }
export interface WsCommandsMsg      { type: 'commands';      sessionId: string; commands: import('@/session/events').Command[] }
export interface WsCommandResultMsg { type: 'commandResult'; sessionId: string; ok: boolean; name?: string; render?: 'help' | 'context' | 'notice' | 'clear'; data?: unknown; error?: string }
export interface WsUpdatePendingMsg   { type: 'updatePending' }
export interface WsUpdateCancelledMsg { type: 'updateCancelled' }

export type WsInboundMsg =
  | WsStatusMsg
  | WsUsageMsg
  | WsRotatedMsg
  | WsErrorMsg
  | WsBatchMsg
  | WsCommandsMsg
  | WsCommandResultMsg
  | WsUpdatePendingMsg
  | WsUpdateCancelledMsg

// ── Callback registry type ───────────────────────────────────────────────────

type EventMap = {
  status:        (msg: WsStatusMsg) => void
  usage:         (msg: WsUsageMsg) => void
  rotated:       (msg: WsRotatedMsg) => void
  error:         (msg: WsErrorMsg) => void
  connectionStateChange: (state: ConnectionState) => void
  sessionEvent:  (e: SessionEvent) => void
  commands:      (msg: WsCommandsMsg) => void
  commandResult: (msg: WsCommandResultMsg) => void
  updatePendingChange: (s: { active: boolean; count: number }) => void
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
  private _updatePending = false
  private _pendingPrompts: { sessionId: string; text: string; attachmentIds?: string[] }[] = []
  private static readonly MAX_PENDING = 50

  constructor(url: string = '/ws') {
    this.url = url
  }

  // ── Public state ────────────────────────────────────────────────────────────

  get connectionState(): ConnectionState { return this._connectionState }
  get activeSessionId(): string | null { return this._activeSessionId }
  get updatePendingState(): { active: boolean; count: number } {
    return { active: this._updatePending, count: this._pendingPrompts.length }
  }

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
      // Re-attach active session so server replays session history
      if (this._activeSessionId) {
        this._send({ type: 'attach', sessionId: this._activeSessionId })
      }
      if (this._pendingPrompts.length) this._flushPending()
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

  interrupt(sessionId: string) {
    this._send({ type: 'interrupt', sessionId })
  }

  prompt(sessionId: string, text: string, attachmentIds?: string[]) {
    // Never silently drop input. Buffer whenever it can't be trusted to land — the
    // server is draining (updatePending) OR the socket isn't open — and flush on the
    // next reconnect (after re-attach). Otherwise send straight through.
    if (this._updatePending || this.ws?.readyState !== WebSocket.OPEN) {
      this._enqueuePrompt({ sessionId, text, attachmentIds })
      return
    }
    this._send({ type: 'prompt', sessionId, text, ...(attachmentIds && attachmentIds.length ? { attachmentIds } : {}) })
  }

  sendInterrupt(sessionId: string) {
    this._send({ type: 'interrupt', sessionId })
  }

  permissionResponse(sessionId: string, permId: string, decision: 'allow' | 'deny' | 'always') {
    this._send({ type: 'permissionResponse', sessionId, permId, decision })
  }

  /** Returns true if the answer was actually written to an open socket. */
  answerQuestion(sessionId: string, questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat' = 'option'): boolean {
    return this._send({ type: 'answerQuestion', sessionId, questionId, selected, mode })
  }

  setPermissionMode(sessionId: string, mode: string) {
    this._send({ type: 'setPermissionMode', sessionId, mode })
  }

  cancelQueued(sessionId: string, index: number) {
    this._send({ type: 'cancelQueued', sessionId, index })
  }

  command(sessionId: string, name: string, args: string) {
    this._send({ type: 'command', sessionId, name, args })
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

  private _emitUpdatePending() {
    this._emit('updatePendingChange', { active: this._updatePending, count: this._pendingPrompts.length })
  }

  private _enqueuePrompt(p: { sessionId: string; text: string; attachmentIds?: string[] }) {
    if (this._pendingPrompts.length >= SessionSocket.MAX_PENDING) {
      const dropped = this._pendingPrompts.shift()
      // No silent caps — tell the user a held message was lost rather than dropping it quietly.
      this._emit('error', {
        type: 'error',
        code: 'PENDING_BUFFER_FULL',
        message: `Too many unsent messages (${SessionSocket.MAX_PENDING}) — the oldest was dropped.`,
      })
      if (dropped) console.warn('[ws] pending-prompt buffer full — dropped oldest', dropped.sessionId)
    }
    this._pendingPrompts.push(p)
    this._emitUpdatePending()
  }

  private _flushPending() {
    const pending = this._pendingPrompts
    this._pendingPrompts = []
    this._updatePending = false
    for (const p of pending) {
      this._send({ type: 'prompt', sessionId: p.sessionId, text: p.text,
        ...(p.attachmentIds && p.attachmentIds.length ? { attachmentIds: p.attachmentIds } : {}) })
    }
    this._emitUpdatePending()
  }

  /** Returns true if the frame was written to an open socket, false if dropped. */
  private _send(msg: object): boolean {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg))
      return true
    }
    return false
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
      ;(cb as (...a: unknown[]) => void)(...(args as unknown[]))
    }
  }

  private _handleInbound(msg: WsInboundMsg) {
    switch (msg.type) {
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
      case 'error':
        this._emit('error', msg)
        break
      case 'batch':
        for (const e of msg.events) {
          this._emit('sessionEvent', e)
        }
        break
      case 'commands':
        this._emit('commands', msg)
        break
      case 'commandResult':
        this._emit('commandResult', msg)
        break
      case 'updatePending':
        this._updatePending = true
        this._emitUpdatePending()
        break
      case 'updateCancelled':
        this._flushPending()
        break
      default:
        // Fan out individual session events sent directly (not wrapped in batch)
        if (SESSION_EVENT_TYPES.has((msg as { type: string }).type)) {
          this._emit('sessionEvent', msg as unknown as SessionEvent)
        }
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

  const attach              = useCallback((sessionId: string) => socket.attach(sessionId), [socket])
  const detach              = useCallback((sessionId: string) => socket.detach(sessionId), [socket])
  const interrupt           = useCallback((sessionId: string) => socket.interrupt(sessionId), [socket])
  const prompt              = useCallback((sessionId: string, text: string, attachmentIds?: string[]) => socket.prompt(sessionId, text, attachmentIds), [socket])
  const sendInterrupt       = useCallback((sessionId: string) => socket.sendInterrupt(sessionId), [socket])
  const permissionResponse  = useCallback((sessionId: string, permId: string, decision: 'allow' | 'deny' | 'always') => socket.permissionResponse(sessionId, permId, decision), [socket])
  const answerQuestion      = useCallback((sessionId: string, questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat' = 'option') => socket.answerQuestion(sessionId, questionId, selected, mode), [socket])
  const setPermissionMode   = useCallback((sessionId: string, mode: string) => socket.setPermissionMode(sessionId, mode), [socket])
  const cancelQueued        = useCallback((sessionId: string, index: number) => socket.cancelQueued(sessionId, index), [socket])
  const command             = useCallback((sessionId: string, name: string, args: string) => socket.command(sessionId, name, args), [socket])

  const on  = useCallback(<K extends keyof EventMap>(event: K, cb: EventMap[K]) => socket.on(event, cb), [socket])
  const off = useCallback(<K extends keyof EventMap>(event: K, cb: EventMap[K]) => socket.off(event, cb), [socket])

  return {
    attach, detach, interrupt,
    prompt, sendInterrupt, permissionResponse, answerQuestion, setPermissionMode, cancelQueued,
    command,
    on, off,
    connectionState,
    socket,
  }
}
