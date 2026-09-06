/**
 * useSessionRuntime — the live runtime for a single session.
 *
 * Lifts the socket wiring, transcript reducer, derived status-strip data, and
 * action handlers out of the SessionView component so they can be shared between
 * the mobile full-screen SessionView and the desktop master-detail layout (where
 * the transcript body and the message composer live in separate parts of the tree).
 *
 * Behavior mirrors what SessionView previously did inline.
 */

import { useEffect, useMemo, useReducer, useState, useCallback, useRef } from 'react'
import { useSessionSocket } from '@/ws/client'
import type {
  ConnectionState, SocketChannel, WsRotatedMsg, WsErrorMsg, WsPromptAckMsg, WsPromptRejectedMsg,
} from '@/ws/client'
import { PROMPT_ACK_TIMEOUT_MS } from '@/session/promptStage'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import type { TranscriptState } from '@/session/reducer'
import type { SessionEvent, Command, PromptContext } from '@/session/events'
import { selectSubAgents, selectVisibleTurns, selectVisibleSubAgents } from '@/session/selectors'
import { remainingContextPct, remainingFromContextUsage } from '@/lib/contextWindow'
import { useSessions, useAccounts, useSettings } from '@/api/hooks'
import { isPreSpawnStatus } from '@/api/types'
import type { UsageWindow } from '@/api/types'
import { usageWindows } from '@/lib/usageWindows'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { fetchOlderTurns } from '@/api/transcript'

// Dispatch payload for attached images: the DB-backed attachment ids.
export type AttachmentIds = string[]

export interface SessionRuntime {
  sessionId: string
  state: TranscriptState
  mode: string
  toolDetailMode: ToolDetailMode
  thinkingCollapsed: boolean
  connectionState: ConnectionState
  /** Re-send the attach frame for this session (used to recover a stuck load). */
  reattach: () => void
  title: string
  /** The reckoned goal autopilot drives toward (effective_goal resolver), for the header. */
  goal: string | null
  /** Where the reckoned goal came from: "explicit" | "inferred" | "initial_prompt". */
  goalSource: string | null
  account: string
  /** The model the user CHOSE for this session (record alias, e.g. "opus"); null =
   *  account default. Optimistically reflects a pick until the record catches up. */
  model: string | null
  /** True while a "switch after this turn" change waits for the in-flight turn to end. */
  modelPending: boolean
  /** The model actually producing (SDK id, e.g. "claude-opus-4-8") — launch model,
   *  refreshed per turn. Read-only: after a switch it lags `model` by one turn. */
  liveModel: string | null
  /** The account's quota WINDOWS, in provider order. Null when the read failed
   *  or no account is attached; an empty list is impossible (an unavailable
   *  snapshot has no windows, so it reads as null here). */
  quota: UsageWindow[] | null
  contextPct: number | null
  subAgents: ReturnType<typeof selectSubAgents>
  hasOpenAction: boolean
  /** True when at least one open permission block is pending (permission blocks only). */
  hasOpenPermission: boolean
  /** Question currently armed for a free-flowing chat reply (next prompt answers it), or null. */
  armedQuestionId: string | null
  /** True when a question is armed for chat — the composer shows a "Reply" affordance. */
  chatArmed: boolean
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  /** Set/clear the chat-armed question (toggles off if the same id is passed again). */
  onChat: (questionId: string) => void
  onPrompt: (text: string, attachmentIds: AttachmentIds, context?: PromptContext) => void
  /** Fire while the user is typing/steering (before send) — refreshes autopilot's human-deference window. */
  onActivity: () => void
  onInterrupt: () => void
  /** Stop the live turn and run this prompt next ("interrupt and post"). Returns
   *  whether the frame was written — false means keep the draft (socket closed). */
  onInterruptAndPrompt: (text: string, attachmentIds: AttachmentIds) => boolean
  onSetMode: (mode: string) => void
  onCancelQueued: (index: number) => void
  commands: Command[]
  commandResult: { render: 'help' | 'context'; data: unknown } | null
  onCommand: (name: string, args: string) => void
  /** Switch model: 'now' stops + restarts a busy turn on the new model; 'after_turn'
   *  hands the model to the SDK without interrupting (applies on the next turn). */
  onSetModel: (id: string, when: 'now' | 'after_turn') => void
  dismissCommandResult: () => void
  /** Fetch and prepend the previous window of older turns (lazy scrollback). */
  onLoadOlder: () => void
}

/**
 * @param channel which WebSocket channel to attach on. 'route' (the default) is the
 *   page's socket; the AI drawer passes 'assistant' so its attach never displaces
 *   the session the page has open.
 */
export function useSessionRuntime(
  sessionId: string,
  initialPrompt?: string,
  channel: SocketChannel = 'route',
): SessionRuntime {
  const {
    attach,
    detach,
    on,
    off,
    connectionState,
    prompt,
    sendInterrupt,
    interruptAndPrompt,
    permissionResponse,
    answerQuestion,
    setPermissionMode,
    cancelQueued,
    command,
    activity,
  } = useSessionSocket(channel)

  const [state, dispatch] = useReducer(transcriptReducer, initialTranscript)

  // Double-buffer: during a gap-heal/reconnect the reducer clears `turns`/`subAgents`
  // and rebuilds them from the replay, but keeps the previously-rendered content in
  // `staleTurns`/`staleSubAgents`. Route ALL view-facing transcript reads through the
  // visible selectors at this single point, then hand the components a `state` whose
  // `turns`/`subAgents` already reflect the double-buffer — so no component needs to
  // know the heal is happening (no blank-then-rebuild flash). Outside a heal this is a
  // no-op: the selectors return the live arrays and `viewState === state`.
  const visibleTurns = selectVisibleTurns(state)
  const visibleSubAgents = selectVisibleSubAgents(state)
  const viewState: TranscriptState = useMemo(
    () =>
      visibleTurns === state.turns && visibleSubAgents === state.subAgents
        ? state
        : { ...state, turns: visibleTurns, subAgents: visibleSubAgents },
    [state, visibleTurns, visibleSubAgents],
  )

  const [mode, setMode] = useState('interactive')
  const [armedQuestionId, setArmedQuestionId] = useState<string | null>(null)
  // Live ack-watchdog timers (one per send), cleared on unmount so a closed session
  // never dispatches into an unmounted reducer.
  const ackTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())
  useEffect(() => {
    const timers = ackTimersRef.current
    return () => { for (const t of timers) clearTimeout(t); timers.clear() }
  }, [])
  const [commands, setCommands] = useState<Command[]>([])
  const [commandResult, setCommandResult] = useState<{ render: 'help' | 'context'; data: unknown } | null>(null)
  const modeRef = useRef('interactive')

  // Attach / detach + subscribe to session events
  useEffect(() => {
    // The session-scope rule for frames whose `sessionId` is OPTIONAL: a frame tagged
    // for a different session is dropped, an untagged one passes through. Untagged
    // means connection-level (or a legacy server) — there is no other view it could
    // belong to, so applying it here is the only sane reading. One helper for THIS
    // hook's handlers, not one copy per handler: the predicate has to mean the same
    // thing for every optional-id frame reaching this view's reducer, and it previously
    // drifted (`error` frames were not scoped at all). It is deliberately not shared
    // with ws/liveBatch.ts's same-shaped check — that one is socket-level (against the
    // socket's `activeSessionId`, and a no-op while that is null), a different scope
    // with a different null rule, so merging them would broaden one of the two.
    // Handlers below whose frame type REQUIRES a sessionId keep the strict `!==` form.
    const isForOtherSession = (frameSessionId: string | undefined): boolean =>
      Boolean(frameSessionId) && frameSessionId !== sessionId

    const onEvent = (e: SessionEvent) => {
      // Replay frames (replayReset, replay content, replayComplete) are stamped
      // server-side with their owning sessionId, just like live events. A stale replay
      // for a session the user has navigated away from must not reach this reducer —
      // its replayReset would wipe this session's transcript to empty (the
      // blank-on-navigation bug).
      if (isForOtherSession((e as { sessionId?: string }).sessionId)) return
      dispatch(e)
    }
    const onRotated = (msg: WsRotatedMsg) => {
      if (msg.sessionId !== sessionId) return
      // Route into the reducer so the rotation is anchored into the transcript
      // timeline (the single source of ordering) and scrolls with history.
      dispatch({ type: '_rotation', from: msg.fromAccount, to: msg.toAccount, reason: msg.reason })
    }
    // I3 — surface direct `error` frames (SESSION_NOT_FOUND, frame_too_large, etc.)
    const onError = (msg: WsErrorMsg) => {
      // Session-scoped errors must not land on whichever session happens to be open:
      // an async failure from another session carries that session's id and would
      // otherwise paint this view's error banner.
      if (isForOtherSession(msg.sessionId)) return
      dispatch({ type: 'error', code: msg.code, message: msg.message, resetsAt: msg.resetsAt ?? null })
    }

    // The edge's positive receipt for a prompt frame (server/bridge/ws.py). This is the
    // frame that closes the "did it even reach the server?" gap: the first one arrives
    // BEFORE the session wake-up, so it lands in milliseconds even when the turn itself
    // is two minutes away.
    const onPromptAck = (msg: WsPromptAckMsg) => {
      if (msg.sessionId !== sessionId) return
      dispatch({ type: '_promptStage', stage: msg.stage })
    }
    // The server refused the prompt and the socket re-queued it for the next flush —
    // the bubble goes back to "held", not to an error: the prompt is still coming.
    const onPromptHeld = (msg: WsPromptRejectedMsg) => {
      if (msg.sessionId !== sessionId) return
      dispatch({ type: '_promptHeld', text: msg.text })
    }

    const onCommands = (msg: { sessionId: string; commands: Command[] }) => {
      if (msg.sessionId !== sessionId) return
      setCommands(msg.commands)
    }
    const onCommandResult = (msg: { sessionId: string; ok: boolean; render?: string; data?: unknown; error?: string }) => {
      if (msg.sessionId !== sessionId) return
      if (!msg.ok) { dispatch({ type: '_notice', text: msg.error ? `Command failed: ${msg.error}` : 'Command failed' }); return }
      if (msg.render === 'clear')  { dispatch({ type: '_clear', text: (msg.data as { text: string })?.text ?? 'Conversation cleared' }); return }
      if (msg.render === 'notice') { dispatch({ type: '_notice', text: (msg.data as { text: string })?.text ?? '' }); return }
      if (msg.render === 'help' || msg.render === 'context') { setCommandResult({ render: msg.render, data: msg.data }) }
    }

    on('sessionEvent', onEvent)
    on('rotated', onRotated)
    on('error', onError)
    on('commands', onCommands)
    on('commandResult', onCommandResult)
    on('promptAck', onPromptAck)
    on('promptHeld', onPromptHeld)
    // Seed the user's prompt optimistically so it shows the instant the session
    // opens, before the SDK echoes it back. The reducer adopts (re-keys) this
    // turn when the real `user` event arrives, so there's no duplicate. The seed
    // is idempotent, so React StrictMode's double-effect is harmless. One-shot at
    // mount: `initialPrompt` is intentionally excluded from the dep array below.
    if (initialPrompt) dispatch({ type: '_seedUserPrompt', text: initialPrompt })
    attach(sessionId)

    return () => {
      off('sessionEvent', onEvent)
      off('rotated', onRotated)
      off('error', onError)
      off('commands', onCommands)
      off('commandResult', onCommandResult)
      off('promptAck', onPromptAck)
      off('promptHeld', onPromptHeld)
      detach(sessionId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // Decision handler for permission cards. Only resolve the card OPTIMISTICALLY when
  // the frame actually left the browser: on a closed socket the send is rejected
  // (permissionResponse returns false and emits NOT_CONNECTED, surfaced as a banner),
  // so we must keep the card interactive rather than falsely marking it resolved (#505).
  const onDecision = useCallback(
    (permId: string, decision: 'allow' | 'deny' | 'always') => {
      if (permissionResponse(sessionId, permId, decision)) {
        dispatch({ type: '_resolvePermission', permId })
      }
    },
    [sessionId, permissionResponse],
  )

  // Answer handler for question cards. Send an answer over WS; on a confirmed
  // send, resolve the question locally and carry the chosen labels on the card.
  // Returns whether the frame actually went out.
  //
  // On the LIVE path the server delivers the answer as a tool result and never
  // echoes it, so the local resolution is the only record of it. The ORPHANED
  // path (a restart killed the waiter — see server/sessions/orphaned_asks.py) is
  // the exception: there is no future left to resolve, so the server delivers the
  // selection as the next turn's PROMPT, which does echo back as a user bubble.
  const deliverAnswer = useCallback(
    (questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat') => {
      const sent = answerQuestion(sessionId, questionId, selected, mode)
      if (!sent) return false
      // Resolve in place — the answered question card stays anchored where it was asked,
      // carrying the chosen labels. (Previously this also appended a user bubble at the
      // transcript tail; the agent's continuation rendered above it, leaving the answer
      // stranded at the bottom. The inline resolution is the single source of truth now.)
      dispatch({ type: '_resolveQuestion', questionId, selected })
      // If this question was armed for chat, the answer resolves it — disarm.
      setArmedQuestionId((cur) => (cur === questionId ? null : cur))
      return true
    },
    [sessionId, answerQuestion],
  )

  const onAnswer = useCallback(
    (questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat' = 'option') => {
      if (deliverAnswer(questionId, selected, mode)) {
        // A confirmed send supersedes any prior "answer not sent" banner from a
        // failed attempt on this question — clear it (only that banner, never an
        // unrelated error already on screen).
        if (state.error?.code === 'answer_not_sent') dispatch({ type: '_clearError' })
        return
      }
      // Socket wasn't open — REJECT, don't buffer. A buffered answer replayed after
      // reconnect is UNSAFE: the server's QuestionRegistry._reconcile resolves a stale
      // answer onto whatever single question is pending NOW, so a late replay can answer
      // the WRONG question with old content (#505). Keep the card interactive so the user
      // can retry once connected, and surface the not-sent error.
      dispatch({
        type: 'error',
        code: 'answer_not_sent',
        message: "Not connected — your answer wasn't sent. Reconnect and try again.",
      })
    },
    [deliverAnswer, state.error],
  )

  // Toggle "armed for chat" for a question: the next composer send becomes its answer.
  const onChat = useCallback(
    (questionId: string) => {
      setArmedQuestionId((cur) => (cur === questionId ? null : questionId))
    },
    [],
  )

  // Composer handlers — when a question is armed for chat, the next message answers
  // that question (mode='chat') and unblocks the turn; otherwise it's a normal prompt.
  // (Chat replies go through the answer channel, which carries no attachments.)
  const onPrompt = useCallback(
    (text: string, attachmentIds: AttachmentIds, context?: PromptContext) => {
      if (armedQuestionId) {
        // A chat reply is delivered through the answer channel — route it through the
        // same gated path as onAnswer so a closed socket REJECTS (visible error, card
        // kept interactive) instead of falsely resolving the question (#505).
        onAnswer(armedQuestionId, [text], 'chat')
        return
      }
      // Send FIRST, then record — the frame's fate is what the bubble reports, and a
      // prompt the socket had to hold must not be drawn as one that is on its way.
      // (Omit the trailing arg entirely when there's no context, rather than passing an
      // explicit `undefined` — callers, and their test mocks, that assert the prompt was
      // sent with exactly 3 args must keep seeing exactly 3.)
      const result = context
        ? prompt(sessionId, text, attachmentIds, context)
        : prompt(sessionId, text, attachmentIds)
      // Track the sent prompt so a reconnect whose replayReset outruns the
      // transcript-file write can re-assert it instead of dropping it silently — and
      // render it immediately as a pending bubble carrying that stage.
      dispatch({ type: '_trackSentPrompt', text, stage: result })
      // Ack watchdog. Fires unconditionally after every send; the reducer decides
      // whether anything is actually still unacknowledged by then, so there is no
      // per-prompt timer bookkeeping to get wrong. Cleared on unmount.
      const timer = setTimeout(() => {
        ackTimersRef.current.delete(timer)
        dispatch({ type: '_promptStalled' })
      }, PROMPT_ACK_TIMEOUT_MS)
      ackTimersRef.current.add(timer)
    },
    [sessionId, prompt, onAnswer, armedQuestionId],
  )
  const onInterrupt = useCallback(
    () => {
      setArmedQuestionId(null)
      sendInterrupt(sessionId)
    },
    [sessionId, sendInterrupt],
  )
  // "Interrupt and post": stop the live turn and run this text next. The server's
  // interrupt denies any parked question, so this is always a fresh prompt (never a
  // chat reply) — disarm locally to match. Returns whether the frame left the browser
  // so the composer can keep the draft on a closed socket (#505 rule for controls).
  const onInterruptAndPrompt = useCallback(
    (text: string, attachmentIds: AttachmentIds): boolean => {
      setArmedQuestionId(null)
      const sent = interruptAndPrompt(sessionId, text, attachmentIds)
      if (sent) dispatch({ type: '_trackSentPrompt', text })
      return sent
    },
    [sessionId, interruptAndPrompt],
  )
  const onSetMode = useCallback(
    (m: string) => {
      // Only apply the mode locally when the frame actually left the browser. On a
      // closed socket the send is rejected (setPermissionMode returns false + emits
      // NOT_CONNECTED), and a stale last-write replay could clobber a newer choice —
      // so don't optimistically show a mode the server never received (#505).
      if (setPermissionMode(sessionId, m)) setMode(m)
    },
    [sessionId, setPermissionMode],
  )
  const onCancelQueued = useCallback(
    (index: number) => cancelQueued(sessionId, index),
    [sessionId, cancelQueued],
  )
  const onCommand = useCallback((name: string, args: string) => command(sessionId, name, args), [sessionId, command])
  const onActivity = useCallback(() => { activity?.(sessionId) }, [sessionId, activity])
  const dismissCommandResult = useCallback(() => setCommandResult(null), [])

  // Keep modeRef current so external keydown handlers can read the latest mode.
  useEffect(() => {
    modeRef.current = mode
  }, [mode])

  // Determine if any open permission/question block exists. Read the VISIBLE turns
  // (via viewState) so these action affordances stay in step with what's on screen
  // during a heal, rather than flickering off while the replay rebuilds `turns`.
  const hasOpenAction = viewState.turns.some((t) =>
    t.blocks.some((b) => b.kind === 'permission' || b.kind === 'question'),
  )
  const hasOpenPermission = viewState.turns.some((t) => t.blocks.some((b) => b.kind === 'permission'))

  // If the armed question is no longer LIVE (resolved/replaced), disarm. A resolved
  // question stays inline as an answered block (live:false), so presence alone is not
  // enough — e.g. the server's questionResolved frame (answered from another tab, or
  // re-resolved after a mid-turn reattach) resolves the block without this client's
  // deliverAnswer ever disarming.
  useEffect(() => {
    if (!armedQuestionId) return
    const stillOpen = viewState.turns.some((t) =>
      t.blocks.some((b) => b.kind === 'question' && b.questionId === armedQuestionId && b.live),
    )
    // eslint-disable-next-line react-hooks/set-state-in-effect -- disarm in sync with transcript changes
    if (!stillOpen) setArmedQuestionId(null)
  }, [viewState.turns, armedQuestionId])

  // ── Status-strip derived data ──────────────────────────────────────────────
  // Account: the session record's account, overridden by the latest rotation.
  const { data: sessions } = useSessions()
  const { data: accounts } = useAccounts()
  const { data: appSettings } = useSettings()
  const toolDetailMode: ToolDetailMode = appSettings?.defaultToolDetail ?? 'auto'
  const thinkingCollapsed = appSettings?.thinkingCollapsed ?? true
  const sessionRecord = sessions?.find((s) => s.id === sessionId)

  // Re-attach when this session leaves a PRE-SPAWN state (`pending_approval` /
  // `queued`) while the view is open. The mount-time attach above ran BEFORE the
  // driver existed, so the bridge took its read-only detached path; the server
  // defers the initial prompt "until the first WS attach" and only a LIVE attach
  // pops + sends it. Without a fresh attach the prompt is orphaned: a Fable session
  // approved from its own view sat empty, and the titler named it after the CLI's
  // `/model` bookkeeping (2026-09-02). Keyed on the record's status — the approve
  // mutation invalidates the sessions query, and the poll covers approvals (or a
  // freed queue slot) that happen elsewhere. The ref carries the session id so a
  // route change can't pair the old session's status with the new one's.
  const recordStatus = sessionRecord?.status
  const prevStatusRef = useRef<{ id: string; status: string | undefined }>({ id: sessionId, status: undefined })
  useEffect(() => {
    const prev = prevStatusRef.current
    prevStatusRef.current = { id: sessionId, status: recordStatus }
    if (prev.id !== sessionId || prev.status === undefined || recordStatus === undefined) return
    if (isPreSpawnStatus(prev.status) && !isPreSpawnStatus(recordStatus)) attach(sessionId)
  }, [sessionId, recordStatus, attach])

  // Header title: prefer the session's name (the AI-generated/auto title, the
  // same value shown on the dashboard card) so the two stay consistent. Fall
  // back to a live transcript `title` event, then the sessionId prefix.
  const title = sessionRecord?.name ?? state.title ?? sessionId.slice(0, 8)
  // The reckoned goal autopilot drives toward, shown beside the title in the
  // header (SessionGoalText hides it when it's just the title fallback).
  const goal = sessionRecord?.effectiveGoal ?? null
  const goalSource = sessionRecord?.goalSource ?? null
  const latestRotation =
    state.rotations.length > 0 ? state.rotations[state.rotations.length - 1].to : null
  const account = latestRotation ?? sessionRecord?.accountName ?? '—'

  // Model: the record's chosen alias (same source the dashboard card shows), with the
  // optimistic pick layered on top until the record catches up. `state.model` is the
  // SDK's producing model and is exposed separately as `liveModel` — binding the picker
  // to it is what made every session look pinned to the account default.
  const recordModel = sessionRecord?.model ?? null
  // Optimistic model pick: the picker shows the requested alias the moment it is
  // chosen instead of waiting for the 5s sessions poll (a lagging picker reads as
  // "snapped back", the very bug this replaces). `seen` is the record's model at pick
  // time; the override retires as soon as the record moves off it (caught up, or
  // changed elsewhere — either way the record is fresher).
  const [modelOverride, setModelOverride] = useState<{ model: string | null; seen: string | null } | null>(null)
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- retire the override once the record moves
    if (modelOverride && recordModel !== modelOverride.seen) setModelOverride(null)
  }, [recordModel, modelOverride])
  const onSetModel = useCallback((id: string, when: 'now' | 'after_turn') => {
    setModelOverride((cur) => ({ model: id === 'default' ? null : id, seen: cur ? cur.seen : recordModel }))
    command(sessionId, 'model', when === 'after_turn' ? `${id} --after-turn` : id)
  }, [sessionId, command, recordModel])
  const model = modelOverride ? modelOverride.model : recordModel
  const modelPending = sessionRecord?.modelPending ?? false
  const liveModel = state.model

  const accountUsage = accounts?.find((a) => a.name === account)?.usage ?? null
  const accountWindows = usageWindows(accountUsage)
  const quota = accountWindows.length > 0 ? accountWindows : null

  // Prefer the SDK's exact context occupancy (the `/context` data) when a live turn
  // has reported it; fall back to the per-call usage approximation for historic or
  // not-yet-run sessions. The approximation must NOT use `usage.input + cacheRead`
  // from a live ResultMessage — that is a per-turn aggregate that reads 0%.
  const contextPct = remainingFromContextUsage(state.contextUsage)
    ?? remainingContextPct(state.usage, state.model)
  // Sub-agent chips derive from the visible turns so they hold steady through a heal.
  const subAgents = selectSubAgents(viewState.turns)

  // Re-send attach to recover from a stuck load (the SessionPlaceholder Retry).
  const reattach = useCallback(() => attach(sessionId), [attach, sessionId])

  // Lazy scrollback: fetch and prepend the previous window of older turns.
  const loadingOlderRef = useRef(false)
  const onLoadOlder = useCallback(() => {
    if (loadingOlderRef.current) return
    if (state.earliestTurn <= 0) return     // already at session start
    loadingOlderRef.current = true
    dispatch({ type: '_setLoadingOlder', value: true })
    fetchOlderTurns(sessionId, state.earliestTurn)
      .then((res) => {
        dispatch({ type: '_prependHistory', events: res.events, earliestTurn: res.earliestTurn })
      })
      .catch(() => {
        dispatch({ type: '_setLoadingOlder', value: false })
      })
      .finally(() => { loadingOlderRef.current = false })
  }, [sessionId, state.earliestTurn])

  return {
    sessionId,
    state: viewState,
    mode,
    toolDetailMode,
    thinkingCollapsed,
    connectionState,
    reattach,
    title,
    goal,
    goalSource,
    account,
    model,
    modelPending,
    liveModel,
    quota,
    contextPct,
    subAgents,
    hasOpenAction,
    hasOpenPermission,
    armedQuestionId,
    chatArmed: armedQuestionId !== null,
    onDecision,
    onAnswer,
    onChat,
    onPrompt,
    onActivity,
    onInterrupt,
    onInterruptAndPrompt,
    onSetMode,
    onCancelQueued,
    commands,
    commandResult,
    onCommand,
    onSetModel,
    dismissCommandResult,
    onLoadOlder,
  }
}
