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
import { clientLogger } from '@/obs/logger'
import { createGapState, handleLiveBatch, catchUpCursor, catchUpEpoch, invalidateCatchUp, type GapState } from './liveBatch'
import { shouldApplyRev } from '@/session/reconcile'

// ── Inbound message shapes ───────────────────────────────────────────────────

export interface WsStatusMsg       { type: 'status';        sessionId: string; status: string; updatedAt?: string; revAt?: string; interruptedByRestart?: boolean }
export interface WsUsageMsg        { type: 'usage';         account: string; session: { pct: number; resetsAt: string | null }; weekly: { pct: number; resetsAt: string | null } }
export interface WsRotatedMsg      { type: 'rotated';       sessionId: string; fromAccount: string; toAccount: string; reason?: string; turnRestarted?: boolean; revAt?: string }
export interface WsErrorMsg        { type: 'error';         sessionId?: string; code: string; message: string; resetsAt?: string | null }
export interface WsBatchMsg        { type: 'batch';         events: SessionEvent[] }
export interface WsCommandsMsg      { type: 'commands';      sessionId: string; commands: import('@/session/events').Command[] }
export interface WsCommandResultMsg { type: 'commandResult'; sessionId: string; ok: boolean; name?: string; render?: 'help' | 'context' | 'notice' | 'clear'; data?: unknown; error?: string }
export interface WsUpdatePendingMsg   { type: 'updatePending' }
export interface WsUpdateCancelledMsg { type: 'updateCancelled' }
export interface WsPromptRejectedMsg   { type: 'promptRejected'; sessionId: string; text: string; attachmentIds?: string[]; reason?: string }

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
  | WsPromptRejectedMsg

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
export const ACTIVITY_THROTTLE_MS = 5000
// How long a socket must stay open before we treat it as healthy and reset the
// reconnect backoff. Shorter than this and we assume it's a flapping connection
// (e.g. a deploy restart accepting then dropping the socket) and let backoff grow.
const CONNECTION_STABLE_MS = 3_000

function jitter(ms: number): number {
  return ms * (0.8 + Math.random() * 0.4)
}

export class SessionSocket {
  private ws: WebSocket | null = null
  private _connectionState: ConnectionState = 'disconnected'
  private _activeSessionId: string | null = null
  private _reconnectAttempt = 0
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private _stableTimer: ReturnType<typeof setTimeout> | null = null
  private _destroyed = false
  private _listeners: { [K in keyof EventMap]?: Set<EventMap[K]> } = {}
  private _queryClient: QueryClient | null = null
  private readonly url: string
  private _updatePending = false
  private _pendingPrompts: { sessionId: string; text: string; attachmentIds?: string[] }[] = []
  private static readonly MAX_PENDING = 50
  private _gap: GapState = createGapState()
  private _lastHealAt = 0
  private static readonly HEAL_COOLDOWN_MS = 2_000

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
      this._setConnectionState('connected')
      // Reset the backoff only once the connection proves STABLE. During a deploy
      // restart the edge can accept the socket then immediately drop it (daemon not
      // ready); resetting the attempt counter on every fleeting open defeats
      // exponential backoff and produces a ~500ms connect/close storm that flickers
      // the reconnecting banner. Arm a timer and reset only if the socket survives it.
      this._clearStableTimer()
      this._stableTimer = setTimeout(() => {
        this._reconnectAttempt = 0
        this._stableTimer = null
      }, CONNECTION_STABLE_MS)
      // Re-attach active session so the server resumes the transcript. Include the
      // wave-3 catch-up cursor when the source stream stayed contiguous — the server
      // then catches us up from the daemon ring (resumeOk) instead of a full replay.
      if (this._activeSessionId) {
        this._send(this._attachFrame(this._activeSessionId))
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
      clientLogger.warn('ws error', { sessionId: this._activeSessionId ?? undefined })
      // onclose will fire right after; let it drive reconnect
    }

    ws.onclose = (e: CloseEvent) => {
      this._clearStableTimer()
      if (this._destroyed) return
      // 1000 = normal, 1001 = navigation; anything else is unexpected and worth logging.
      const expected = e.code === 1000 || e.code === 1001
      if (!expected) {
        clientLogger.warn(`ws closed code=${e.code}`, {
          sessionId: this._activeSessionId ?? undefined,
          stack: e.reason || undefined,
        })
      }
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
    this._clearStableTimer()
    this.ws?.close()
    this.ws = null
  }

  private _clearStableTimer() {
    if (this._stableTimer !== null) {
      clearTimeout(this._stableTimer)
      this._stableTimer = null
    }
  }

  // ── Outbound messages ───────────────────────────────────────────────────────

  attach(sessionId: string) {
    this._activeSessionId = sessionId
    // A public attach is a FRESH user-initiated view: the reducer mounts empty
    // (key={sessionId}), so any surviving catch-up cursor for this session is stale.
    // Invalidate it BEFORE sending so that if this attach is dropped on a closed socket,
    // the reconnect onopen re-attach (_attachFrame) omits sinceSrcSeq and full-replays,
    // instead of getting resumeOk + tail-only against an empty transcript (Hole 1). This
    // is intentionally NOT done on the mid-view reconnect path (onopen with no intervening
    // public attach) — that's the legitimate resumeOk case (reducer still intact) — nor by
    // the raw-_send heal, which must leave the cursor for its own contiguity check.
    invalidateCatchUp(this._gap, sessionId)
    this._send({ type: 'attach', sessionId })
  }

  detach(sessionId: string) {
    if (this._activeSessionId === sessionId) this._activeSessionId = null
    this._send({ type: 'detach', sessionId })
  }

  interrupt(sessionId: string): boolean {
    return this._sendControl({ type: 'interrupt', sessionId })
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

  sendInterrupt(sessionId: string): boolean {
    return this._sendControl({ type: 'interrupt', sessionId })
  }

  permissionResponse(sessionId: string, permId: string, decision: 'allow' | 'deny' | 'always'): boolean {
    return this._sendControl({ type: 'permissionResponse', sessionId, permId, decision })
  }

  /** Returns true if the answer was actually written to an open socket. When the
   *  socket is closed it is REJECTED (returns false + emits NOT_CONNECTED) — the
   *  answer is NOT buffered for late replay, because a stale answer can be reconciled
   *  onto the wrong live question (see #505 and useSessionRuntime.onAnswer). */
  answerQuestion(sessionId: string, questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat' = 'option'): boolean {
    return this._sendControl({ type: 'answerQuestion', sessionId, questionId, selected, mode })
  }

  private _lastActivitySent = 0

  /** Fire-and-forget "user is steering" ping (typing / interacting before send). Refreshes the
   *  server's human-deference window so autopilot stands down. Throttled to one per
   *  ACTIVITY_THROTTLE_MS; silent best-effort (dropped when the socket is closed — a missed ping
   *  just means the window isn't refreshed that tick, which is harmless). */
  activity(sessionId: string): void {
    const now = Date.now()
    if (now - this._lastActivitySent < ACTIVITY_THROTTLE_MS) return
    if (this._send({ type: 'activity', sessionId })) this._lastActivitySent = now
  }

  setPermissionMode(sessionId: string, mode: string): boolean {
    return this._sendControl({ type: 'setPermissionMode', sessionId, mode })
  }

  cancelQueued(sessionId: string, index: number): boolean {
    return this._sendControl({ type: 'cancelQueued', sessionId, index })
  }

  command(sessionId: string, name: string, args: string): boolean {
    return this._sendControl({ type: 'command', sessionId, name, args })
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

  /** Send a CONTROL frame (interrupt, permission response, answer, set-mode, cancel,
   *  command). Unlike attach/detach/prompt — which self-heal via re-attach or the
   *  prompt queue — these frames are NOT safe to buffer-and-replay late: the server
   *  handlers are not id-idempotent against a changed live state (an interrupt is not
   *  id-scoped and would kill a fresh turn; cancelQueued is index-addressed; a stale
   *  answer can be reconciled onto the wrong pending question). So when the socket is
   *  closed we REJECT with a visible error instead of silently dropping the frame —
   *  the single closed-socket handling point for every control method (#505). Mirrors
   *  the PENDING_BUFFER_FULL emit so the reducer renders one consistent error surface. */
  private _sendControl(msg: object): boolean {
    if (this._send(msg)) return true
    this._emit('error', {
      type: 'error',
      code: 'NOT_CONNECTED',
      message: 'Not connected — action not sent. Reconnect and try again.',
    })
    return false
  }

  private _setConnectionState(state: ConnectionState) {
    if (this._connectionState === state) return
    this._connectionState = state
    this._emit('connectionStateChange', state)
  }

  /** Self-heal a detected live-stream gap by detaching then re-attaching the active
   *  session, which triggers the server's replayReset → replay → replayComplete rebuild.
   *
   *  WHY detach before attach: the server's _handle_attach now tears down THIS socket's
   *  existing forwarding task and re-runs the attach path even for an already-live session
   *  (a re-attach is never a silent no-op — see server/bridge/ws.py _handle_attach). So the
   *  bare attach alone would heal; the explicit detach first is kept because it makes the
   *  teardown intent obvious and matches the server's read-it-off-live_tasks path exactly.
   *  The detach cancels only THIS socket's forwarding task (the driver keeps running and
   *  buffering); the subsequent attach re-opens the stream and full-replays (or catches up
   *  from the cursor). The server processes frames sequentially (single awaited receive
   *  loop), so detach fully completes before attach — no race, no double forwarding task.
   *
   *  We use raw _send() rather than the public detach()/attach() methods because those
   *  mutate _activeSessionId; the heal must leave it intact.
   *
   *  Rate-limited so a persistently gappy stream cannot storm the server. */
  private _healActiveSession() {
    const sid = this._activeSessionId
    if (!sid) return
    const now = Date.now()
    if (now - this._lastHealAt < SessionSocket.HEAL_COOLDOWN_MS) return
    this._lastHealAt = now
    clientLogger.warn('ws gap detected — healing transcript via detach+attach', { sessionId: sid })
    this._send({ type: 'detach', sessionId: sid })
    // Catch-up cursor when contiguous → server resumes from the ring (resumeOk); a gap
    // that broke contiguity (the usual heal trigger) yields null → full replay.
    this._send(this._attachFrame(sid))
  }

  /** Build an `attach` frame, adding the wave-3 `sinceSrcSeq` catch-up cursor when the
   *  session's source stream has stayed contiguous (see catchUpCursor). Omitting the
   *  cursor makes the server full-replay from the durable transcript — the safe default,
   *  used for the initial user-initiated attach and whenever contiguity is unknown/broken.
   *
   *  When a cursor is sent, also echo the daemon `epoch` that minted it as `sinceEpoch`
   *  (#497): the server rejects a cursor from a previous epoch (a daemon restart resets the
   *  srcSeq space to 0) and full-replays, so a stale cross-epoch cursor can never silently
   *  skip the new epoch's early events. A missing epoch is simply omitted → the server also
   *  full-replays (conservative). */
  private _attachFrame(
    sessionId: string,
  ): { type: 'attach'; sessionId: string; sinceSrcSeq?: number; sinceEpoch?: string } {
    const cursor = catchUpCursor(this._gap, sessionId)
    if (cursor === null) return { type: 'attach', sessionId }
    const epoch = catchUpEpoch(this._gap, sessionId)
    return epoch !== null
      ? { type: 'attach', sessionId, sinceSrcSeq: cursor, sinceEpoch: epoch }
      : { type: 'attach', sessionId, sinceSrcSeq: cursor }
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
        this._patchSessionStatus(msg.sessionId, msg.status as Session['status'], msg.updatedAt, msg.revAt, msg.interruptedByRestart)
        break
      case 'usage':
        this._emit('usage', msg)
        this._patchAccountUsage(msg)
        break
      case 'rotated':
        this._emit('rotated', msg)
        this._patchSessionAccount(msg.sessionId, msg.toAccount, msg.revAt)
        break
      case 'error':
        this._emit('error', msg)
        break
      case 'batch': {
        // replayReset re-baselining happens INSIDE handleLiveBatch, in frame
        // order — a pre-scan here would apply the reset before a stale
        // old-epoch event sharing the batch, letting its seq re-poison the
        // fresh baseline (see handleLiveBatch's stale-straddle note).
        const { dispatch, needsHeal, srcSeqGaps } = handleLiveBatch(msg.events, {
          activeSessionId: this._activeSessionId,
          state: this._gap,
        })
        for (const e of dispatch) this._emit('sessionEvent', e)
        // srcSeq loss telemetry (audit W2.3): record daemon→edge→browser holes to the
        // obs sink WITHOUT healing — this is diagnostic only, the foundation for the
        // wave-3 cursor protocol. The per-attach `seq` above still owns dedup/heal.
        for (const g of srcSeqGaps) {
          clientLogger.warn(`transcript.srcseq.gap prev=${g.prev} got=${g.got}`, {
            sessionId: g.sessionId,
          })
        }
        if (needsHeal) this._healActiveSession()
        break
      }
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
      case 'promptRejected':
        // Server refused this prompt (deploy drain). Re-queue it so it flushes on
        // the next reconnect/cancel instead of being lost, and enter holding.
        this._updatePending = true
        this._enqueuePrompt({ sessionId: msg.sessionId, text: msg.text, attachmentIds: msg.attachmentIds })
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

  private _patchSessionStatus(sessionId: string, status: Session['status'], updatedAt?: string, revAt?: string, interruptedByRestart?: boolean) {
    const qc = this._queryClient
    if (!qc) return
    qc.setQueryData<Session[]>(QK.sessions, (old) => {
      if (!old) return old
      return old.map((s) => {
        if (s.id !== sessionId) return s
        // Version guard: ignore a status frame strictly older than what's cached (a stale
        // or duplicate frame that raced a newer poll/patch). Missing tokens → apply.
        if (!shouldApplyRev(revAt, s.revAt)) return s
        return { ...s, status,
                 ...(updatedAt ? { updatedAt } : {}),
                 ...(revAt ? { revAt } : {}),
                 // Issue #434: the status frame carries the live restart-interruption flag
                 // (cleared on resume) so the pill relabels without waiting for the 5s poll.
                 ...(interruptedByRestart !== undefined ? { interruptedByRestart } : {}) }
      })
    })
  }

  private _patchSessionAccount(sessionId: string, accountName: string, revAt?: string) {
    const qc = this._queryClient
    if (!qc) return
    // A rotation is not a fresh turn — update the account only, never updatedAt. The
    // rev token IS bumped by the rotation (server/sessions/orchestrator.py _reassign)
    // and must be applied here so reconcileSessions (web/src/session/reconcile.ts) can
    // tell this patch is newer than a poll response snapshotted just before the
    // rotation persisted — otherwise the next 5s poll silently reverts the account
    // badge to the old account. Same rev-guard as _patchSessionStatus: a strictly
    // older/stale revAt is rejected; a missing token on either side still applies.
    qc.setQueryData<Session[]>(QK.sessions, (old) => {
      if (!old) return old
      return old.map((s) => {
        if (s.id !== sessionId) return s
        if (!shouldApplyRev(revAt, s.revAt)) return s
        return { ...s, accountName, ...(revAt ? { revAt } : {}) }
      })
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
  const activity            = useCallback((sessionId: string) => socket.activity(sessionId), [socket])

  const on  = useCallback(<K extends keyof EventMap>(event: K, cb: EventMap[K]) => socket.on(event, cb), [socket])
  const off = useCallback(<K extends keyof EventMap>(event: K, cb: EventMap[K]) => socket.off(event, cb), [socket])

  return {
    attach, detach, interrupt,
    prompt, sendInterrupt, permissionResponse, answerQuestion, setPermissionMode, cancelQueued,
    command, activity,
    on, off,
    connectionState,
    socket,
  }
}
