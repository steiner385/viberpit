import type {
  SessionEvent, ToolKind, EvUser, EvTurnStart, EvTextDelta, EvThinkingDelta, EvToolCall, EvToolResult, QueuedItem,
} from './events'
import type { ContextUsageExact } from '@/lib/contextWindow'

/** Sentinel id for the optimistic user turn seeded before the SDK echoes the prompt. */
export const OPTIMISTIC_USER_ID = 'optimistic:user'

// ── Block types ───────────────────────────────────────────────────────────────

export type Block =
  | { kind: 'text';       id: string; text: string; ts?: number }
  | { kind: 'thinking';   id: string; text: string; ts?: number }
  | { kind: 'image';      id: string; images: Array<{ url: string; mime: string; name: string }>; ts?: number }
  | { kind: 'tool';       id: string; toolId: string; name: string; toolKind: ToolKind; input: Record<string, unknown>; result?: { ok: boolean; payload: Record<string, unknown>; ts?: number }; ts?: number }
  | { kind: 'permission'; id: string; permId: string; tool: string; toolKind: ToolKind; input: Record<string, unknown>; plan: boolean; ts?: number }
  | { kind: 'question';   id: string; questionId: string; header: string; question: string; options: Array<Record<string, unknown>>; multiSelect: boolean; live?: boolean; ts?: number }
  | { kind: 'notice';     id: string; text: string; ts?: number }
  | { kind: 'error';      id: string; code: string; message: string; resetsAt?: string | null; ts?: number }

// ── Turn ─────────────────────────────────────────────────────────────────────

export interface Turn { id: string; role: 'user' | 'assistant'; blocks: Block[] }

/** A self-contained conversation thread (the main transcript, or one sub-agent). */
export interface SubThread { turns: Turn[]; openBlockId: string | null }

// ── Rotation marker ────────────────────────────────────────────────────────────

/**
 * An account-rotation event, interleaved into the transcript timeline.
 *
 * `afterTurnId` is the id of the last turn that existed when the rotation
 * arrived (or null if no turn had arrived yet). The transcript renders each
 * rotation immediately after its anchor turn, reproducing real arrival order
 * without needing per-turn timestamps. A null anchor renders at the very top.
 */
export interface RotationMarker {
  from: string
  to: string
  reason?: string
  afterTurnId: string | null
}

// ── State ─────────────────────────────────────────────────────────────────────

/** Latest result's token/cost footprint (consumed by lib/contextWindow helpers). */
export interface UsageSnapshot {
  input: number
  output: number
  cacheRead: number
  costUsd: number
}

export interface TranscriptState {
  turns: Turn[]
  openBlockId: string | null    // id of the open text/thinking block in the active assistant turn
  status: { state: string; count: number }
  title: string | null
  model: string | null          // current model id (from sessionMeta / replay)
  usage: UsageSnapshot | null   // most recent result's token/cost snapshot
  contextUsage: ContextUsageExact | null  // exact ctx-window occupancy (SDK /context data); preferred over `usage` for the ctx% readout
  replaying: boolean            // true until replayComplete
  earliestTurn: number          // lowest turn ordinal currently loaded (0 = at session start)
  totalTurns: number            // total turns in the on-disk transcript
  loadingOlder: boolean         // a prepend fetch is in flight
  error: { code: string; message: string; resetsAt?: string | null } | null
  queued: QueuedItem[]           // prompts waiting in the server-side queue
  rotations: RotationMarker[]   // account rotations, anchored into the timeline
  subAgents: Record<string, SubThread>   // keyed by the spawning Task toolId
  latestSuggestions: string[]  // next-prompt chips for the current idle turn
  condense: { active: boolean; expectedMs: number } | null
}

export const initialTranscript: TranscriptState = {
  turns: [],
  openBlockId: null,
  status: { state: 'idle', count: 0 },
  title: null,
  model: null,
  usage: null,
  contextUsage: null,
  replaying: true,
  earliestTurn: 0,
  totalTurns: 0,
  loadingOlder: false,
  error: null,
  queued: [],
  rotations: [],
  subAgents: {},
  latestSuggestions: [],
  condense: null,
}

/** Coerce a possibly-missing numeric usage field to a finite number (0 when absent). */
function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

// ── Local (UI-only) synthetic actions ────────────────────────────────────────

export type LocalAction =
  | { type: '_resolvePermission'; permId: string }
  | { type: '_resolveQuestion';   questionId: string }
  | { type: '_clearError' }
  | { type: '_seedUserPrompt';    text: string }
  | { type: '_rotation';          from: string; to: string; reason?: string }
  | { type: '_notice'; text: string }
  | { type: '_clear';  text: string }
  | { type: '_prependHistory'; events: SessionEvent[]; earliestTurn: number }
  | { type: '_setLoadingOlder'; value: boolean }

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Return the index of a turn by id, or -1 if not found. */
function findTurnIdx(turns: Turn[], id: string): number {
  return turns.findIndex((t) => t.id === id)
}

/**
 * Return a new turns array with the turn at `idx` replaced by the result of
 * `fn`. Turns before and after are referentially identical (important for
 * React.memo stability).
 */
function replaceTurn(turns: Turn[], idx: number, fn: (t: Turn) => Turn): Turn[] {
  const next = [...turns]
  next[idx] = fn(next[idx])
  return next
}

/**
 * Ensure a turn exists at turnId; if it doesn't, append an empty assistant
 * turn. Returns [nextTurns, idx].
 */
function ensureAssistantTurn(turns: Turn[], turnId: string): [Turn[], number] {
  const idx = findTurnIdx(turns, turnId)
  if (idx !== -1) return [turns, idx]
  return [[...turns, { id: turnId, role: 'assistant', blocks: [] }], turns.length]
}

/**
 * Build the blocks for a user turn: the text block, plus one image block when the
 * event carries attachments. Single-sourced so every user-turn path (append,
 * optimistic-seed adoption, sub-thread) renders attachments identically. The image
 * `url` is used verbatim as an <img> src — a serve URL live, a `data:` URL on replay.
 */
function userTurnBlocks(turnId: string, text: string, images?: EvUser['images'], ts?: number): Block[] {
  const blocks: Block[] = [{ kind: 'text', id: `${turnId}:u`, text, ts }]
  if (images && images.length) blocks.push({ kind: 'image', id: `${turnId}:img`, images, ts })
  return blocks
}

// ── Thread-scoped reducer ─────────────────────────────────────────────────────

type BlockEvent = EvUser | EvTurnStart | EvTextDelta | EvThinkingDelta | EvToolCall | EvToolResult

const BLOCK_EVENT_TYPES: ReadonlySet<string> = new Set([
  'user', 'turnStart', 'textDelta', 'thinkingDelta', 'toolCall', 'toolResult',
])

/** Apply a block-bearing event to a single thread. Returns the same `thread` reference on a no-op. */
function applyToThread(thread: SubThread, ev: BlockEvent): SubThread {
  switch (ev.type) {
    case 'user': {
      const { turnId, text } = ev
      const blocks = userTurnBlocks(turnId, text, ev.images, ev.ts)
      const idx = findTurnIdx(thread.turns, turnId)
      const turns =
        idx === -1
          ? [...thread.turns, { id: turnId, role: 'user' as const, blocks }]
          : replaceTurn(thread.turns, idx, (t) => ({ ...t, blocks }))
      return { turns, openBlockId: null }
    }

    case 'turnStart': {
      const idx = findTurnIdx(thread.turns, ev.turnId)
      if (idx !== -1) {
        if (thread.openBlockId === null) return thread
        return { turns: thread.turns, openBlockId: null }
      }
      return {
        turns: [...thread.turns, { id: ev.turnId, role: 'assistant', blocks: [] }],
        openBlockId: null,
      }
    }

    case 'textDelta': {
      const { turnId, text } = ev
      const [turns, idx] = ensureAssistantTurn(thread.turns, turnId)
      const turn = turns[idx]
      const openBlock = thread.openBlockId
        ? turn.blocks.find((b) => b.id === thread.openBlockId && b.kind === 'text')
        : undefined
      if (openBlock && openBlock.kind === 'text') {
        const nextTurns = replaceTurn(turns, idx, (t) => ({
          ...t,
          blocks: t.blocks.map((b) =>
            b.id === openBlock.id && b.kind === 'text' ? { ...b, text: b.text + text } : b),
        }))
        return { turns: nextTurns, openBlockId: thread.openBlockId }
      }
      const blockId = `${turnId}:t${turn.blocks.length}`
      const nextTurns = replaceTurn(turns, idx, (t) => ({
        ...t, blocks: [...t.blocks, { kind: 'text', id: blockId, text, ts: ev.ts }],
      }))
      return { turns: nextTurns, openBlockId: blockId }
    }

    case 'thinkingDelta': {
      const { turnId, text } = ev
      const [turns, idx] = ensureAssistantTurn(thread.turns, turnId)
      const turn = turns[idx]
      const openBlock = thread.openBlockId
        ? turn.blocks.find((b) => b.id === thread.openBlockId && b.kind === 'thinking')
        : undefined
      if (openBlock && openBlock.kind === 'thinking') {
        const nextTurns = replaceTurn(turns, idx, (t) => ({
          ...t,
          blocks: t.blocks.map((b) =>
            b.id === openBlock.id && b.kind === 'thinking' ? { ...b, text: b.text + text } : b),
        }))
        return { turns: nextTurns, openBlockId: thread.openBlockId }
      }
      const blockId = `${turnId}:th${turn.blocks.length}`
      const nextTurns = replaceTurn(turns, idx, (t) => ({
        ...t, blocks: [...t.blocks, { kind: 'thinking', id: blockId, text, ts: ev.ts }],
      }))
      return { turns: nextTurns, openBlockId: blockId }
    }

    case 'toolCall': {
      const { turnId, toolId, name, toolKind, input } = ev
      const [turns, idx] = ensureAssistantTurn(thread.turns, turnId)
      const turn = turns[idx]
      if (turn.blocks.some((b) => b.kind === 'tool' && b.toolId === toolId)) {
        if (thread.openBlockId === null && turns === thread.turns) return thread
        return { turns, openBlockId: null }
      }
      const newBlock: Block = { kind: 'tool', id: toolId, toolId, name, toolKind, input, ts: ev.ts }
      const nextTurns = replaceTurn(turns, idx, (t) => ({ ...t, blocks: [...t.blocks, newBlock] }))
      return { turns: nextTurns, openBlockId: null }
    }

    case 'toolResult': {
      const { toolId, ok, payload } = ev
      let found = false
      const nextTurns = thread.turns.map((turn) => {
        const blockIdx = turn.blocks.findIndex((b) => b.kind === 'tool' && b.toolId === toolId)
        if (blockIdx === -1) return turn
        found = true
        return {
          ...turn,
          blocks: turn.blocks.map((b, i) =>
            i === blockIdx && b.kind === 'tool' ? { ...b, result: { ok, payload, ts: ev.ts } } : b),
        }
      })
      if (!found) return thread
      return { turns: nextTurns, openBlockId: thread.openBlockId }
    }
  }
}

// ── Reducer ───────────────────────────────────────────────────────────────────

export function transcriptReducer(
  state: TranscriptState,
  ev: SessionEvent | LocalAction,
): TranscriptState {
  // Route block-bearing events tagged with a parentToolUseId to the spawning
  // Task's sub-thread. Untagged (main-transcript) block events fall through to
  // the full switch below, which carries the richer main-thread behaviour
  // (optimistic-seed adoption, question live-upgrade, inline error blocks).
  if (BLOCK_EVENT_TYPES.has(ev.type)) {
    const pid = (ev as { parentToolUseId?: string | null }).parentToolUseId
    if (pid) {
      const cur = state.subAgents[pid] ?? { turns: [], openBlockId: null }
      const next = applyToThread(cur, ev as BlockEvent)
      if (next === cur) return state
      return { ...state, subAgents: { ...state.subAgents, [pid]: next } }
    }
  }

  switch (ev.type) {
    // ── user ─────────────────────────────────────────────────────────────────
    case 'user': {
      const { turnId, text } = ev
      const blocks = userTurnBlocks(turnId, text, ev.images, ev.ts)

      // Adopt an optimistic seed turn if present: re-key it in place to the real
      // server turnId instead of appending a duplicate. Unconditional adoption is
      // safe because a brand-new session starts with an empty replay — the first
      // user event is always the seeded prompt — and only one optimistic turn exists.
      const optIdx = findTurnIdx(state.turns, OPTIMISTIC_USER_ID)
      if (optIdx !== -1) {
        const nextTurns = replaceTurn(state.turns, optIdx, () => ({
          id: turnId,
          role: 'user' as const,
          blocks,
        }))
        return { ...state, turns: nextTurns, openBlockId: null, latestSuggestions: [] }
      }

      const idx = findTurnIdx(state.turns, turnId)
      let nextTurns: Turn[]
      if (idx === -1) {
        // Append new user turn
        nextTurns = [...state.turns, { id: turnId, role: 'user', blocks }]
      } else {
        // Upsert: rebuild the existing turn's blocks (text + any attachments)
        nextTurns = replaceTurn(state.turns, idx, (t) => ({ ...t, blocks }))
      }
      return { ...state, turns: nextTurns, openBlockId: null, latestSuggestions: [] }
    }

    // ── turnStart ─────────────────────────────────────────────────────────────
    case 'turnStart': {
      const { turnId } = ev
      const idx = findTurnIdx(state.turns, turnId)
      if (idx !== -1) {
        // Idempotent — turn already exists. If openBlockId is already null, return state unchanged.
        if (state.openBlockId === null) return state
        return { ...state, openBlockId: null, latestSuggestions: [] }
      }
      return {
        ...state,
        turns: [...state.turns, { id: turnId, role: 'assistant', blocks: [] }],
        openBlockId: null,
        latestSuggestions: [],
      }
    }

    // ── textDelta ─────────────────────────────────────────────────────────────
    case 'textDelta': {
      const { turnId, text } = ev
      const [turns, idx] = ensureAssistantTurn(state.turns, turnId)
      const turn = turns[idx]

      // Check if the currently open block is a text block in this turn
      const openBlock = state.openBlockId
        ? turn.blocks.find((b) => b.id === state.openBlockId && b.kind === 'text')
        : undefined

      if (openBlock && openBlock.kind === 'text') {
        // Append to existing text block
        const nextTurns = replaceTurn(turns, idx, (t) => ({
          ...t,
          blocks: t.blocks.map((b) =>
            b.id === openBlock.id && b.kind === 'text'
              ? { ...b, text: b.text + text }
              : b,
          ),
        }))
        return { ...state, turns: nextTurns }
      } else {
        // Open a new text block
        const blockId = `${turnId}:t${turn.blocks.length}`
        const newBlock: Block = { kind: 'text', id: blockId, text, ts: ev.ts }
        const nextTurns = replaceTurn(turns, idx, (t) => ({
          ...t,
          blocks: [...t.blocks, newBlock],
        }))
        return { ...state, turns: nextTurns, openBlockId: blockId }
      }
    }

    // ── thinkingDelta ─────────────────────────────────────────────────────────
    case 'thinkingDelta': {
      const { turnId, text } = ev
      const [turns, idx] = ensureAssistantTurn(state.turns, turnId)
      const turn = turns[idx]

      // Check if the currently open block is a thinking block in this turn
      const openBlock = state.openBlockId
        ? turn.blocks.find((b) => b.id === state.openBlockId && b.kind === 'thinking')
        : undefined

      if (openBlock && openBlock.kind === 'thinking') {
        // Append to existing thinking block
        const nextTurns = replaceTurn(turns, idx, (t) => ({
          ...t,
          blocks: t.blocks.map((b) =>
            b.id === openBlock.id && b.kind === 'thinking'
              ? { ...b, text: b.text + text }
              : b,
          ),
        }))
        return { ...state, turns: nextTurns }
      } else {
        // Open a new thinking block (closes any open text block)
        const blockId = `${turnId}:th${turn.blocks.length}`
        const newBlock: Block = { kind: 'thinking', id: blockId, text, ts: ev.ts }
        const nextTurns = replaceTurn(turns, idx, (t) => ({
          ...t,
          blocks: [...t.blocks, newBlock],
        }))
        return { ...state, turns: nextTurns, openBlockId: blockId }
      }
    }

    // ── toolCall ─────────────────────────────────────────────────────────────
    case 'toolCall': {
      const { turnId, toolId, name, toolKind, input } = ev
      const [turns, idx] = ensureAssistantTurn(state.turns, turnId)
      const turn = turns[idx]

      // Idempotent by toolId. If turn already has this tool and openBlockId is already null, return state unchanged.
      if (turn.blocks.some((b) => b.kind === 'tool' && b.toolId === toolId)) {
        if (state.openBlockId === null && turns === state.turns) return state
        return { ...state, turns, openBlockId: null }
      }

      const newBlock: Block = { kind: 'tool', id: toolId, toolId, name, toolKind, input, ts: ev.ts }
      const nextTurns = replaceTurn(turns, idx, (t) => ({
        ...t,
        blocks: [...t.blocks, newBlock],
      }))
      return { ...state, turns: nextTurns, openBlockId: null }
    }

    // ── toolResult ────────────────────────────────────────────────────────────
    case 'toolResult': {
      const { toolId, ok, payload } = ev
      // Find the tool block across all turns
      let found = false
      const nextTurns = state.turns.map((turn) => {
        const blockIdx = turn.blocks.findIndex((b) => b.kind === 'tool' && b.toolId === toolId)
        if (blockIdx === -1) return turn
        found = true
        const newBlocks = turn.blocks.map((b, i) =>
          i === blockIdx && b.kind === 'tool' ? { ...b, result: { ok, payload, ts: ev.ts } } : b,
        )
        return { ...turn, blocks: newBlocks }
      })
      if (!found) return state // IGNORE orphan toolResult
      return { ...state, turns: nextTurns }
    }

    // ── permissionRequest ─────────────────────────────────────────────────────
    case 'permissionRequest': {
      const { permId, turnId, tool, toolKind, input, plan } = ev
      const [turns, idx] = ensureAssistantTurn(state.turns, turnId)
      const turn = turns[idx]

      // Idempotent by permId
      if (turn.blocks.some((b) => b.kind === 'permission' && b.permId === permId)) {
        return { ...state, turns }
      }

      const newBlock: Block = { kind: 'permission', id: permId, permId, tool, toolKind, input, plan, ts: ev.ts }
      const nextTurns = replaceTurn(turns, idx, (t) => ({
        ...t,
        blocks: [...t.blocks, newBlock],
      }))
      return { ...state, turns: nextTurns }
    }

    // ── question ─────────────────────────────────────────────────────────────
    case 'question': {
      const { questionId, turnId, header, question, options, multiSelect } = ev
      const [turns, idx] = ensureAssistantTurn(state.turns, turnId)
      const turn = turns[idx]

      // Idempotent by questionId — but the server re-emits a still-pending
      // question after replayComplete. The replay copy was created live:false
      // (replaying was true); the re-emit (replaying now false) must upgrade it
      // to live:true, otherwise a question pending across a refresh/reconnect
      // renders read-only forever. Only ever flip false→true; never downgrade.
      const existing = turn.blocks.find((b) => b.kind === 'question' && b.questionId === questionId)
      if (existing) {
        if (existing.kind === 'question' && !existing.live && !state.replaying) {
          const nextTurns = replaceTurn(turns, idx, (t) => ({
            ...t,
            blocks: t.blocks.map((b) =>
              b.kind === 'question' && b.questionId === questionId ? { ...b, live: true } : b,
            ),
          }))
          return { ...state, turns: nextTurns }
        }
        return { ...state, turns }
      }

      const newBlock: Block = { kind: 'question', id: questionId, questionId, header, question, options, multiSelect, live: !state.replaying, ts: ev.ts }
      const nextTurns = replaceTurn(turns, idx, (t) => ({
        ...t,
        blocks: [...t.blocks, newBlock],
      }))
      return { ...state, turns: nextTurns }
    }

    // ── sessionMeta ───────────────────────────────────────────────────────────
    case 'sessionMeta': {
      return { ...state, model: ev.model }
    }

    // ── title ─────────────────────────────────────────────────────────────────
    case 'title': {
      return { ...state, title: ev.title }
    }

    // ── status ────────────────────────────────────────────────────────────────
    case 'status': {
      return { ...state, status: { state: ev.state, count: ev.count } }
    }

    // ── turnEnd ───────────────────────────────────────────────────────────────
    case 'turnEnd': {
      // Snapshot the latest result's usage (token footprint of the current
      // context) and cumulative cost. A turnEnd with no usage leaves the
      // previous snapshot intact.
      if (ev.usage == null) return { ...state, openBlockId: null }
      const u = ev.usage
      const usage: UsageSnapshot = {
        input: num(u.input_tokens),
        output: num(u.output_tokens),
        cacheRead: num(u.cache_read_input_tokens),
        costUsd: num(ev.costUsd),
      }
      return { ...state, openBlockId: null, usage }
    }

    // ── contextUsage ──────────────────────────────────────────────────────────
    case 'contextUsage': {
      // Exact context-window occupancy from the SDK (same data as `/context`),
      // emitted after each live turn settles AND re-emitted from the persisted record
      // on attach (server/bridge/ws.py) so a reloaded/idle session shows real remaining
      // context instead of the model-string approximation. Preferred over the `usage`-based
      // approximation for the ctx% readout — see useSessionRuntime. The transcript replay
      // itself never carries it, so it is intentionally preserved across replayReset (like
      // rotations/queue) rather than cleared.
      const maxTokens = num(ev.maxTokens)
      if (!(maxTokens > 0)) return state
      return { ...state, contextUsage: { usedTokens: num(ev.usedTokens), maxTokens } }
    }

    // ── replayReset ───────────────────────────────────────────────────────────
    case 'replayReset': {
      // The server prepends this to every transcript replay (sent on each
      // attach, including reconnect re-attaches). Streamed text/thinking deltas
      // are append-only — without a reset, a second replay re-appends a full
      // copy of every assistant block (the "duplicating content" bug). Clear the
      // transcript-rebuildable state so the following replay events rebuild it
      // cleanly. Live-only state that the replay does NOT re-emit (rotations,
      // queue, banner) is preserved.
      return { ...state, turns: [], openBlockId: null, subAgents: {}, replaying: true,
               earliestTurn: 0, totalTurns: 0, loadingOlder: false }
    }

    // ── replayComplete ────────────────────────────────────────────────────────
    case 'replayComplete': {
      return { ...state, replaying: false,
               earliestTurn: ev.earliestTurn ?? state.earliestTurn,
               totalTurns: ev.totalTurns ?? state.totalTurns }
    }

    // ── _setLoadingOlder ──────────────────────────────────────────────────────
    case '_setLoadingOlder': {
      return { ...state, loadingOlder: ev.value }
    }

    // ── _prependHistory ───────────────────────────────────────────────────────
    case '_prependHistory': {
      const olderTurns = buildTurnsFromEvents(ev.events)
      // Drop any older turn whose id already exists (defensive; windows are disjoint server-side).
      const existing = new Set(state.turns.map((t) => t.id))
      const fresh = olderTurns.filter((t) => !existing.has(t.id))
      return { ...state, turns: [...fresh, ...state.turns],
               earliestTurn: ev.earliestTurn, loadingOlder: false }
    }

    // ── error ─────────────────────────────────────────────────────────────────
    case 'error': {
      // Always raise the banner (the prominent, dismissable surface). When the error
      // is anchored to a turn, ALSO drop an inline block into that turn's timeline so
      // it stays visible in place and a later error can't erase it (the banner is a
      // single slot; the timeline is append-only).
      const banner = { code: ev.code, message: ev.message, resetsAt: ev.resetsAt }
      if (!ev.turnId) return { ...state, error: banner }
      const [withTurn, idx] = ensureAssistantTurn(state.turns, ev.turnId)
      const turn = withTurn[idx]
      const errBlock: Block = {
        kind: 'error', id: `${ev.turnId}:err:${turn.blocks.length}`,
        code: ev.code, message: ev.message, resetsAt: ev.resetsAt, ts: ev.ts,
      }
      const turns = replaceTurn(withTurn, idx, (t) => ({ ...t, blocks: [...t.blocks, errBlock] }))
      return { ...state, turns, error: banner }
    }

    // ── queueUpdate ───────────────────────────────────────────────────────────
    case 'queueUpdate': {
      return { ...state, queued: ev.items }
    }

    // ── condensing ────────────────────────────────────────────────────────────
    case 'condensing': {
      return ev.active
        ? { ...state, condense: { active: true, expectedMs: ev.expectedMs ?? 12000 } }
        : { ...state, condense: null }
    }

    // ── suggestions ───────────────────────────────────────────────────────────
    case 'suggestions':
      return { ...state, latestSuggestions: ev.items }

    // ── Local synthetic actions ────────────────────────────────────────────────

    case '_resolvePermission': {
      const { permId } = ev
      let changed = false
      const nextTurns = state.turns.map((turn) => {
        const hasIt = turn.blocks.some((b) => b.kind === 'permission' && b.permId === permId)
        if (!hasIt) return turn
        changed = true
        return { ...turn, blocks: turn.blocks.filter((b) => !(b.kind === 'permission' && b.permId === permId)) }
      })
      if (!changed) return state // No matching permission block found; return unchanged state
      return { ...state, turns: nextTurns }
    }

    case '_rotation': {
      // Anchor the rotation to the last turn present right now, so the
      // transcript can render it inline at the chronologically correct spot.
      const afterTurnId = state.turns.length > 0 ? state.turns[state.turns.length - 1].id : null
      const marker: RotationMarker = { from: ev.from, to: ev.to, reason: ev.reason, afterTurnId }
      return { ...state, rotations: [...state.rotations, marker] }
    }

    case '_resolveQuestion': {
      const { questionId } = ev
      let changed = false
      const nextTurns = state.turns.map((turn) => {
        const hasIt = turn.blocks.some((b) => b.kind === 'question' && b.questionId === questionId)
        if (!hasIt) return turn
        changed = true
        return { ...turn, blocks: turn.blocks.filter((b) => !(b.kind === 'question' && b.questionId === questionId)) }
      })
      if (!changed) return state // No matching question block found; return unchanged state
      return { ...state, turns: nextTurns }
    }

    case '_notice': {
      const id = `notice:${state.turns.length}`
      return {
        ...state,
        turns: [...state.turns, { id, role: 'assistant', blocks: [{ kind: 'notice', id: `${id}:n`, text: ev.text }] }],
        openBlockId: null,
      }
    }

    case '_clearError': {
      // Dismiss a surfaced banner (e.g. the transient "answer queued" notice
      // once a buffered answer has been resent). Ref-stable when already clear.
      return state.error ? { ...state, error: null } : state
    }

    case '_seedUserPrompt': {
      // Idempotent: never seed twice (guards React StrictMode double-invoke).
      if (findTurnIdx(state.turns, OPTIMISTIC_USER_ID) !== -1) return state
      const block: Block = { kind: 'text', id: `${OPTIMISTIC_USER_ID}:u`, text: ev.text }
      return {
        ...state,
        turns: [...state.turns, { id: OPTIMISTIC_USER_ID, role: 'user', blocks: [block] }],
        openBlockId: null,
      }
    }

    case '_clear': {
      const id = 'notice:clear'
      return {
        ...initialTranscript,
        replaying: false,
        turns: [{ id, role: 'assistant', blocks: [{ kind: 'notice', id: `${id}:n`, text: ev.text }] }],
      }
    }

    default:
      return state
  }
}

/**
 * Fold a window of replayed events into a fresh transcript and return just its
 * turns. Reuses transcriptReducer so history paging never grows a second
 * turn-builder (single source of truth for event→Turn translation).
 */
export function buildTurnsFromEvents(events: SessionEvent[]): Turn[] {
  const seeded: TranscriptState = { ...initialTranscript, replaying: true }
  const final = events.reduce(transcriptReducer, seeded)
  return final.turns
}
