/**
 * SessionSocket — single authenticated WebSocket to /ws that multiplexes terminal I/O
 * and control/event messages across all sessions (R7).
 *
 * Reconnect: on close/error, exponential backoff (500ms → 10s, jittered).
 * On reopen, re-sends `attach` for the active session so the server replays session history (SC-008).
 */

import type { QueryClient } from '@tanstack/react-query'
import type { AccountUsage, Session } from '@/api/types'
import { QK } from '@/api/hooks'
import type { SessionEvent, PromptContext } from '@/session/events'
import { SESSION_EVENT_TYPES } from '@/session/events'
import type { PromptStage } from '@/session/promptStage'
import { noteSent, noteStage, noteHeld, clearFlight } from '@/lib/promptFlight'
import { clientLogger } from '@/obs/logger'
import { createGapState, handleLiveBatch, catchUpCursor, catchUpEpoch, invalidateCatchUp, type GapState } from './liveBatch'
import { revIsNewer, shouldApplyRev } from '@/session/reconcile'
import { RESTING_BEFORE_TURN } from '@/session/statusTier'

// ── Inbound message shapes ───────────────────────────────────────────────────

export interface WsStatusMsg       { type: 'status';        sessionId: string; status: string; updatedAt?: string; revAt?: string; interruptedByRestart?: boolean; pausedReason?: string | null; pausedUntil?: string | null; usageStalled?: boolean }
export interface WsUsageMsg        { type: 'usage';         account: string; usage: AccountUsage }
export interface WsRotatedMsg      { type: 'rotated';       sessionId: string; fromAccount: string; toAccount: string; reason?: string; turnRestarted?: boolean; revAt?: string }
export interface WsErrorMsg        { type: 'error';         sessionId?: string; code: string; message: string; resetsAt?: string | null }
export interface WsBatchMsg        { type: 'batch';         events: SessionEvent[] }
export interface WsCommandsMsg      { type: 'commands';      sessionId: string; commands: import('@/session/events').Command[] }
export interface WsCommandResultMsg { type: 'commandResult'; sessionId: string; ok: boolean; name?: string; render?: 'help' | 'context' | 'notice' | 'clear'; data?: unknown; error?: string }
export interface WsUpdatePendingMsg   { type: 'updatePending' }
export interface WsUpdateCancelledMsg { type: 'updateCancelled' }
export interface WsPromptRejectedMsg   { type: 'promptRejected'; sessionId: string; text: string; attachmentIds?: string[]; reason?: string }
/** Positive receipt for a prompt frame — the counterpart to promptRejected. See
 *  server/bridge/ws.py; `stage` is the PromptStage vocabulary (session/promptStage.ts). */
export interface WsPromptAckMsg { type: 'promptAck'; sessionId: string; stage: PromptStage }

/** What `SessionSocket.prompt()` did with the text: put it on the wire, or hold it here. */
export type PromptSendResult = 'sent' | 'buffered'

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
  | WsPromptAckMsg

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
  promptAck:     (msg: WsPromptAckMsg) => void
  promptHeld:    (msg: WsPromptRejectedMsg) => void
  // Fired when the server sends close code 1012 (daemon epoch change / planned restart).
  // Consumers can show a recovery overlay immediately rather than waiting for a health poll.
  epochRestart: () => void
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
// Rev-watermark liveness probe. The 5s sessions poll and lifecycle status patches
// keep the cached row's revAt fresh over channels independent of the per-session
// transcript stream; if that rev advances while no transcript frames arrive, the
// stream is stale (wedged daemon reader, dead forwarding task) — a state the seq
// gap-heal can never see, because total silence contains no gaps.
export const LIVENESS_CHECK_MS = 5_000
export const LIVENESS_STALE_MS = 15_000

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
  private _pendingPrompts: { sessionId: string; text: string; attachmentIds?: string[]; context?: PromptContext }[] = []
  private static readonly MAX_PENDING = 50
  private _gap: GapState = createGapState()
  // revAt the transcript stream was last demonstrably synced to: whenever session
  // frames arrive we adopt the cached row's revAt as "seen". Compared against the
  // cache by _livenessCheck.
  private _syncedRevAt: string | null = null
  private _lastSessionFrameAt = 0
  private _livenessTimer: ReturnType<typeof setInterval> | null = null
  // Consecutive probe-triggered heals that delivered no session frames. The probe's
  // job is to recover a RECOVERABLE stream; once this many full replays in a row have
  // produced nothing, more will not help and the 5s retry is pure harm — a whole
  // browser population detach/attach-storming a server that is already sick. So the
  // probe stands down for this session until frames actually arrive (which resets the
  // counter, along with a fresh public attach). Deliberately NOT exponential backoff:
  // there is no useful middle ground between "this heals it" and "stop".
  private _fruitlessProbeHeals = 0
  private static readonly LIVENESS_MAX_FRUITLESS_HEALS = 3
  private _lastHealAt = 0
  private static readonly HEAL_COOLDOWN_MS = 2_000
  // Heal-loop detection: timestamps of recent heals (pruned to HEAL_LOOP_WINDOW_MS).
  // Reaching HEAL_LOOP_THRESHOLD heals inside the window means healing is not
  // converging — each full replay is itself producing a fresh gap — which the
  // per-heal cooldown alone can sustain forever, silently.
  private _healTimes: number[] = []
  private static readonly HEAL_LOOP_WINDOW_MS = 60_000
  private static readonly HEAL_LOOP_THRESHOLD = 5
  // srcSeq loss telemetry aggregation: per-session cumulative gap counts, flushed to the
  // obs sink at most once per SRC_GAP_LOG_INTERVAL_MS per session (first gap logs
  // immediately) so a gap storm reports a count instead of flooding client_logs.
  private _srcGapCounts: Map<string, number> = new Map()
  private _srcGapLastLogAt: Map<string, number> = new Map()
  private static readonly SRC_GAP_LOG_INTERVAL_MS = 10_000

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
    // Idempotent: connect() runs again on every reconnect, and one interval for the
    // socket's lifetime is what we want — the probe itself is inert unless there is
    // an active session on a connected socket.
    if (this._livenessTimer === null) {
      this._livenessTimer = setInterval(() => this._livenessCheck(), LIVENESS_CHECK_MS)
    }

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
        // Re-baseline the liveness probe's silence clock with the re-attach. Across a
        // laptop sleep or a deploy restart _lastSessionFrameAt is minutes stale while
        // revAt advanced over the still-running HTTP poll; if the catch-up dispatches
        // zero frames (a legitimate resumeOk on a quiet session), the very next tick
        // would heal 0–5s after the attach we are sending right here — from every open
        // browser at once, at a server that just came back. The probe gets a fresh
        // LIVENESS_STALE_MS to observe the reconnected stream before judging it.
        this._lastSessionFrameAt = Date.now()
        this._send(this._attachFrame(this._activeSessionId))
      }
      if (this._pendingPrompts.length) this._flushPending()
    }

    ws.onmessage = (event) => this._onRawMessage(event.data as string)

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
      // 1012 = daemon epoch change (planned restart). Signal immediately so UI can show
      // a recovery overlay without waiting for the next /healthz poll (which lags 10s).
      if (e.code === 1012) this._emit('epochRestart')
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
    if (this._livenessTimer !== null) {
      clearInterval(this._livenessTimer)
      this._livenessTimer = null
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
    // Fresh view: baseline the probe so pre-attach staleness can't instantly fire.
    this._lastSessionFrameAt = Date.now()
    this._syncedRevAt = this._cachedRevAt(sessionId)
    this._fruitlessProbeHeals = 0
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

  /**
   * Send a prompt. Returns which of the two things actually happened, so the caller
   * can say so: `'sent'` = the frame left the browser, `'buffered'` = held here until
   * the next reconnect. Both are safe — input is never dropped — but they are very
   * different things to be waiting on, and rendering them identically is what left a
   * held prompt indistinguishable from a slow one.
   */
  prompt(sessionId: string, text: string, attachmentIds?: string[], context?: PromptContext): PromptSendResult {
    // Never silently drop input. Buffer whenever it can't be trusted to land — the
    // server is draining (updatePending) OR the socket isn't open — and flush on the
    // next reconnect (after re-attach). Otherwise send straight through.
    if (this._updatePending || this.ws?.readyState !== WebSocket.OPEN) {
      this._enqueuePrompt({ sessionId, text, attachmentIds, context })
      noteSent(sessionId, 'buffered')
      return 'buffered'
    }
    this._send({ type: 'prompt', sessionId, text,
      ...(attachmentIds && attachmentIds.length ? { attachmentIds } : {}),
      ...(context ? { context } : {}) })
    noteSent(sessionId, 'sent')
    return 'sent'
  }

  sendInterrupt(sessionId: string): boolean {
    return this._sendControl({ type: 'interrupt', sessionId })
  }

  /** "Interrupt and post": stop the in-flight turn and run this prompt next, instead
   *  of queueing it behind the turn. A CONTROL frame (not a buffered prompt): replayed
   *  late it would cut short whatever turn is running by then — the interrupt rule. */
  interruptAndPrompt(sessionId: string, text: string, attachmentIds?: string[]): boolean {
    return this._sendControl({ type: 'prompt', sessionId, text,
      ...(attachmentIds && attachmentIds.length ? { attachmentIds } : {}), interrupt: true })
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

  private _enqueuePrompt(p: { sessionId: string; text: string; attachmentIds?: string[]; context?: PromptContext }) {
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
        ...(p.attachmentIds && p.attachmentIds.length ? { attachmentIds: p.attachmentIds } : {}),
        ...(p.context ? { context: p.context } : {}) })
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
   *  Rate-limited so a persistently gappy stream cannot storm the server.
 *
 *  Returns whether a heal was actually written to the wire (false = no active session,
 *  or suppressed by the cooldown), so callers that count their own heals — the liveness
 *  probe's fruitless-heal budget — count real ones only. */
  private _healActiveSession(): boolean {
    const sid = this._activeSessionId
    if (!sid) return false
    const now = Date.now()
    if (now - this._lastHealAt < SessionSocket.HEAL_COOLDOWN_MS) return false
    this._lastHealAt = now
    // Heal-loop detection: the cooldown rate-limits heals but cannot tell converging
    // (one heal, done) from diverging (every replay ends in a fresh gap). Count heals in
    // a rolling window; escalate to error EXACTLY at the threshold crossing (not on every
    // subsequent heal) so the loop is visible in client_logs without becoming a storm of
    // its own. Behavior is unchanged — the heal below still runs; full replay from the
    // durable transcript remains the safest available action.
    this._healTimes = this._healTimes.filter(
      (t) => now - t < SessionSocket.HEAL_LOOP_WINDOW_MS)
    this._healTimes.push(now)
    if (this._healTimes.length === SessionSocket.HEAL_LOOP_THRESHOLD) {
      clientLogger.error(
        `transcript.heal.loop count=${this._healTimes.length} windowMs=${SessionSocket.HEAL_LOOP_WINDOW_MS}`,
        { sessionId: sid })
    }
    clientLogger.warn('ws gap detected — healing transcript via detach+attach', { sessionId: sid })
    this._send({ type: 'detach', sessionId: sid })
    // Catch-up cursor when contiguous → server resumes from the ring (resumeOk); a gap
    // that broke contiguity (the usual heal trigger) yields null → full replay.
    this._send(this._attachFrame(sid))
    return true
  }

  /** The cached sessions-list row for a session, or null. The single read point for
   *  everything the socket needs off the poll's snapshot (rev token AND status). */
  private _cachedRow(sessionId: string | null): Session | null {
    if (!sessionId || !this._queryClient) return null
    const rows = this._queryClient.getQueryData<Session[]>(QK.sessions)
    return rows?.find((s) => s.id === sessionId) ?? null
  }

  private _cachedRevAt(sessionId: string | null): string | null {
    return this._cachedRow(sessionId)?.revAt ?? null
  }

  /** Rev-watermark liveness probe (see LIVENESS_CHECK_MS above). Runs on an
   *  interval; also directly callable by tests. Heals at most once per rev
   *  advance: the watermark is bumped BEFORE the heal, so a heal whose replay
   *  produces no frames cannot re-trigger on the same rev.
   *
   *  THREE conditions are required, and each one exists to keep a full replay
   *  (which is what a heal costs) off a healthy socket:
   *
   *  1. A baseline we actually established. `revIsNewer(x, null)` is TRUE by design
   *     (reconcile.ts: an absent existing token is no basis to reject), so a null
   *     `_syncedRevAt` reads as "the rev advanced" against literally anything. On a
   *     cold deep-link `attach()` runs before the sessions query resolves, so the
   *     baseline IS null; the first poll would then "advance" the rev and heal a
   *     perfectly healthy stream at t=15s. Adopt the first non-null cached read as
   *     the baseline instead, and judge nothing until we have one.
   *  2. The rev advanced past that baseline.
   *  3. The cached row is NOT resting. `revAt` is `iso_utc(updated_at)`
   *     (server/sessions/types.py) and `updated_at` is a ROW-WRITE token that bumps on
   *     every persist (orchestrator.py) — auto-naming, sub-agent persistence, archive
   *     and usage-stall bookkeeping, queue updates — most of which emit no transcript
   *     event at all. On a finished session that background write is indistinguishable
   *     from a stalled stream by rev alone, and the false heal is expensive twice over:
   *     `attach()` invalidated the catch-up cursor, so it is a FULL replay, and it logs
   *     a warn that pollutes the very heal signal that is supposed to mean something.
   *     Status is the right discriminator, not a patch: this probe exists for a wedged
   *     daemon reader starving the stream of a session that is ACTUALLY RUNNING, and in
   *     that failure mode the independent poll shows it working. Resting + silent is the
   *     normal, correct state. Reuses RESTING_BEFORE_TURN — the existing status-vocabulary
   *     source of truth — rather than a second list to drift from it. */
  private _livenessCheck(): void {
    const sid = this._activeSessionId
    if (!sid || this._connectionState !== 'connected') return
    // Budget spent: this session's stream did not come back for three replays. Stay
    // quiet until frames arrive (see _fruitlessProbeHeals).
    if (this._fruitlessProbeHeals >= SessionSocket.LIVENESS_MAX_FRUITLESS_HEALS) return
    const row = this._cachedRow(sid)
    const revAt = row?.revAt ?? null
    if (this._syncedRevAt === null) {
      // (1) No baseline yet — adopt, never judge. A null baseline is the absence of
      // evidence, not evidence of an advance.
      if (revAt !== null) this._syncedRevAt = revAt
      return
    }
    if (!revIsNewer(revAt, this._syncedRevAt)) return                 // (2)
    if (!row || RESTING_BEFORE_TURN.has(row.status)) return           // (3)
    if (Date.now() - this._lastSessionFrameAt < LIVENESS_STALE_MS) return
    clientLogger.warn('transcript.liveness.stale — rev advanced with a silent stream, healing', {
      sessionId: sid,
    })
    this._syncedRevAt = revAt
    if (this._healActiveSession()) this._fruitlessProbeHeals++
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

  /** Parse + route one raw inbound frame. Extracted from `ws.onmessage` so tests can
   *  drive it directly.
   *
   *  TWO stages, two catches, deliberately. The single `catch {}` this replaced wrapped
   *  `JSON.parse` AND `_handleInbound`, so a throw from anywhere downstream (a listener,
   *  the batch pipeline) was swallowed as if the wire frame had been malformed — the
   *  worse of the two bugs, and one a `parse_error` label would actively misdirect: the
   *  next debugger would go hunting a server encoding bug that never existed. Each stage
   *  now reports under its own name, so the log says which one actually threw.
   *
   *  Neither stage rethrows. A bad frame must never kill the socket (unchanged
   *  behaviour) — but it must no longer vanish without a trace: a truncated frame or a
   *  server encoding bug used to resurface days later as an unexplained "chat got
   *  stuck" report with nothing in client_logs. */
  private _onRawMessage(data: string): void {
    let msg: WsInboundMsg
    try {
      msg = JSON.parse(data) as WsInboundMsg
    } catch (e) {
      // Frame length, not the frame: the payload may carry transcript text, and this
      // sink is shipped to the server. (clientLogger's `extra` is a fixed
      // Partial<ClientLogEntry> envelope, so the detail rides in the message.)
      clientLogger.warn(`ws.message.parse_error dataLen=${data?.length ?? 0}`, {
        sessionId: this._activeSessionId ?? undefined,
        stack: String(e),
      })
      return
    }
    try {
      this._handleInbound(msg)
    } catch (e) {
      const frame = msg as { type?: string; sessionId?: string } | null
      clientLogger.warn(`ws.message.handler_error type=${frame?.type ?? 'unknown'}`, {
        sessionId: frame?.sessionId ?? this._activeSessionId ?? undefined,
        stack: e instanceof Error ? (e.stack ?? String(e)) : String(e),
      })
    }
  }

  private _handleInbound(msg: WsInboundMsg) {
    switch (msg.type) {
      case 'status':
        this._emit('status', msg)
        this._patchSessionStatus(msg.sessionId, msg.status as Session['status'], msg.updatedAt, msg.revAt, msg.interruptedByRestart, msg)
        // The send has been superseded by real news about the session — stop narrating
        // it. This top-level broadcast (unlike the attached event stream) arrives for
        // sessions the user has navigated away from, which is precisely the case the
        // list pill exists for: otherwise a prompt fired and left behind would leave a
        // "Sent — starting…" pill up forever.
        if (!RESTING_BEFORE_TURN.has(msg.status)) clearFlight(msg.sessionId)
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
        if (dispatch.length > 0) {
          // Frames for the active session arrived → the stream is live; adopt the
          // cache's current rev as synced so the liveness probe stays quiet, and give
          // the probe its fruitless-heal budget back (a heal that DID deliver frames,
          // or an unaided recovery, both land here).
          this._lastSessionFrameAt = Date.now()
          this._syncedRevAt = this._cachedRevAt(this._activeSessionId) ?? this._syncedRevAt
          this._fruitlessProbeHeals = 0
        }
        // srcSeq loss telemetry (audit W2.3): record daemon→edge→browser holes to the
        // obs sink WITHOUT healing — this is diagnostic only, the foundation for the
        // wave-3 cursor protocol. The per-attach `seq` above still owns dedup/heal.
        // Aggregated per session: the first gap logs immediately (with its prev/got
        // anchor), further gaps within SRC_GAP_LOG_INTERVAL_MS only bump a cumulative
        // count that rides the next flush — a storm reports "total=N", not N entries.
        for (const g of srcSeqGaps) {
          const total = (this._srcGapCounts.get(g.sessionId) ?? 0) + 1
          this._srcGapCounts.set(g.sessionId, total)
          const lastLog = this._srcGapLastLogAt.get(g.sessionId)
          const gapNow = Date.now()
          if (lastLog === undefined
              || gapNow - lastLog >= SessionSocket.SRC_GAP_LOG_INTERVAL_MS) {
            this._srcGapLastLogAt.set(g.sessionId, gapNow)
            clientLogger.warn(
              `transcript.srcseq.gap prev=${g.prev} got=${g.got} total=${total}`,
              { sessionId: g.sessionId })
          }
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
      case 'promptAck':
        // Both surfaces: the store (session list, mounted or not) and the channel
        // (the open transcript's reducer).
        noteStage(msg.sessionId, msg.stage)
        this._emit('promptAck', msg)
        break
      case 'promptRejected':
        // Server refused this prompt (deploy drain). Re-queue it so it flushes on
        // the next reconnect/cancel instead of being lost, and enter holding.
        this._updatePending = true
        this._enqueuePrompt({ sessionId: msg.sessionId, text: msg.text, attachmentIds: msg.attachmentIds })
        // Tell the transcript too: its bubble has been showing "Sending…" for a frame
        // that came back. Without this the re-queue is correct but invisible.
        noteHeld(msg.sessionId)
        this._emit('promptHeld', msg)
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

  private _patchSessionStatus(sessionId: string, status: Session['status'], updatedAt?: string, revAt?: string, interruptedByRestart?: boolean, stall?: Pick<WsStatusMsg, 'pausedReason' | 'pausedUntil' | 'usageStalled'>) {
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
                 ...(interruptedByRestart !== undefined
                     ? { interruptedByRestart, ...(interruptedByRestart ? {} : { resumable: false }) }
                     : {}),
                 // Stall fields ride the same frame so the "Waiting for capacity" pill
                 // and the Rotate-stalled count update without waiting for the poll.
                 ...(stall?.usageStalled !== undefined
                     ? { usageStalled: stall.usageStalled, pausedReason: stall.pausedReason ?? null, pausedUntil: stall.pausedUntil ?? null }
                     : {}) }
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
        // A rotation ends whatever pause parked the session.
        return { ...s, accountName, usageStalled: false, pausedReason: null, pausedUntil: null, ...(revAt ? { revAt } : {}) }
      })
    })
  }

  private _patchAccountUsage(msg: WsUsageMsg) {
    const qc = this._queryClient
    if (!qc) return
    // Import Account type inline to avoid circular imports
    type AccountLike = { name: string; usage: AccountUsage | null }
    qc.setQueryData<AccountLike[]>(QK.accounts, (old) => {
      if (!old) return old
      // Replace the whole snapshot: the number of windows is provider-dependent,
      // so merging field-by-field would leave a stale window behind when a
      // provider reports fewer than it did before.
      return old.map((a) => (a.name === msg.account ? { ...a, usage: msg.usage } : a))
    })
  }
}

// ── Channel singletons ───────────────────────────────────────────────────────
// One socket per CHANNEL, shared across all useSessionSocket callers on it.
//
// A SessionSocket tracks a single `_activeSessionId`, so two consumers sharing
// one instance fight over it: attaching the assistant thread would detach the
// page's session (and detach it again on unmount). The AI drawer therefore gets
// its own channel. Both connect to the same `/ws` endpoint — the server already
// accepts several sockets per identity (that is what multi-tab is).

/** 'route' carries the session the page has open; 'assistant' the AI drawer's thread. */
export type SocketChannel = 'route' | 'assistant'

const _sockets: Partial<Record<SocketChannel, SessionSocket>> = {}

export function getSocket(channel: SocketChannel = 'route'): SessionSocket {
  let socket = _sockets[channel]
  if (!socket) {
    socket = new SessionSocket('/ws')
    _sockets[channel] = socket
    // Created lazily, so the assistant socket only opens once the drawer asks for it.
    socket.connect()
  }
  return socket
}

/** The page's session socket — the original accessor, now the 'route' channel. */
export function getSessionSocket(): SessionSocket {
  return getSocket('route')
}

/** Test-only: drop every channel's socket so the next getSocket() builds a fresh one. */
export function __resetSocketsForTest(): void {
  for (const channel of Object.keys(_sockets) as SocketChannel[]) {
    _sockets[channel]?.destroy()
    delete _sockets[channel]
  }
}

// ── React hook ───────────────────────────────────────────────────────────────

import { useEffect, useState, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'

export function useSessionSocket(channel: SocketChannel = 'route') {
  const queryClient = useQueryClient()
  const socket = getSocket(channel)

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
  const prompt              = useCallback((sessionId: string, text: string, attachmentIds?: string[], context?: PromptContext) => socket.prompt(sessionId, text, attachmentIds, context), [socket])
  const sendInterrupt       = useCallback((sessionId: string) => socket.sendInterrupt(sessionId), [socket])
  const interruptAndPrompt  = useCallback((sessionId: string, text: string, attachmentIds?: string[]) => socket.interruptAndPrompt(sessionId, text, attachmentIds), [socket])
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
    prompt, sendInterrupt, interruptAndPrompt, permissionResponse, answerQuestion, setPermissionMode, cancelQueued,
    command, activity,
    on, off,
    connectionState,
    socket,
  }
}
