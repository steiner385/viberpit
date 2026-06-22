import type {
  SessionEvent, ToolKind, EvUser, EvTurnStart, EvTextDelta, EvThinkingDelta, EvToolCall, EvToolResult,
} from './events'

// ── Block types ───────────────────────────────────────────────────────────────

export type Block =
  | { kind: 'text';       id: string; text: string }
  | { kind: 'thinking';   id: string; text: string }
  | { kind: 'tool';       id: string; toolId: string; name: string; toolKind: ToolKind; input: Record<string, unknown>; result?: { ok: boolean; payload: Record<string, unknown> } }
  | { kind: 'permission'; id: string; permId: string; tool: string; toolKind: ToolKind; input: Record<string, unknown>; plan: boolean }
  | { kind: 'question';   id: string; questionId: string; header: string; question: string; options: Array<Record<string, unknown>>; multiSelect: boolean }

// ── Turn ─────────────────────────────────────────────────────────────────────

export interface Turn { id: string; role: 'user' | 'assistant'; blocks: Block[] }

/** A self-contained conversation thread (the main transcript, or one sub-agent). */
export interface SubThread { turns: Turn[]; openBlockId: string | null }

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
  replaying: boolean            // true until replayComplete
  error: { code: string; message: string; resetsAt?: string | null } | null
  queued: string[]              // texts of prompts waiting in the server-side queue
  subAgents: Record<string, SubThread>   // keyed by the spawning Task toolId
}

export const initialTranscript: TranscriptState = {
  turns: [],
  openBlockId: null,
  status: { state: 'idle', count: 0 },
  title: null,
  model: null,
  usage: null,
  replaying: true,
  error: null,
  queued: [],
  subAgents: {},
}

/** Coerce a possibly-missing numeric usage field to a finite number (0 when absent). */
function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

// ── Local (UI-only) synthetic actions ────────────────────────────────────────

export type LocalAction =
  | { type: '_resolvePermission'; permId: string }
  | { type: '_resolveQuestion';   questionId: string }

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
      const userBlock: Block = { kind: 'text', id: `${turnId}:u`, text }
      const idx = findTurnIdx(thread.turns, turnId)
      const turns =
        idx === -1
          ? [...thread.turns, { id: turnId, role: 'user' as const, blocks: [userBlock] }]
          : replaceTurn(thread.turns, idx, (t) => ({
              ...t,
              blocks: t.blocks.map((b) => (b.kind === 'text' && b.id === `${turnId}:u` ? userBlock : b)),
            }))
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
        ...t, blocks: [...t.blocks, { kind: 'text', id: blockId, text }],
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
        ...t, blocks: [...t.blocks, { kind: 'thinking', id: blockId, text }],
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
      const newBlock: Block = { kind: 'tool', id: toolId, toolId, name, toolKind, input }
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
            i === blockIdx && b.kind === 'tool' ? { ...b, result: { ok, payload } } : b),
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
  // Route block-bearing events to a thread: the spawning Task's sub-thread when
  // tagged with parentToolUseId, otherwise the main transcript.
  if (BLOCK_EVENT_TYPES.has(ev.type)) {
    const pid = (ev as { parentToolUseId?: string | null }).parentToolUseId
    if (pid) {
      const cur = state.subAgents[pid] ?? { turns: [], openBlockId: null }
      const next = applyToThread(cur, ev as BlockEvent)
      if (next === cur) return state
      return { ...state, subAgents: { ...state.subAgents, [pid]: next } }
    }
    const cur: SubThread = { turns: state.turns, openBlockId: state.openBlockId }
    const next = applyToThread(cur, ev as BlockEvent)
    if (next.turns === state.turns && next.openBlockId === state.openBlockId) return state
    return { ...state, turns: next.turns, openBlockId: next.openBlockId }
  }

  switch (ev.type) {
    // ── permissionRequest ─────────────────────────────────────────────────────
    case 'permissionRequest': {
      const { permId, turnId, tool, toolKind, input, plan } = ev
      const [turns, idx] = ensureAssistantTurn(state.turns, turnId)
      const turn = turns[idx]

      // Idempotent by permId
      if (turn.blocks.some((b) => b.kind === 'permission' && b.permId === permId)) {
        return { ...state, turns }
      }

      const newBlock: Block = { kind: 'permission', id: permId, permId, tool, toolKind, input, plan }
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

      // Idempotent by questionId
      if (turn.blocks.some((b) => b.kind === 'question' && b.questionId === questionId)) {
        return { ...state, turns }
      }

      const newBlock: Block = { kind: 'question', id: questionId, questionId, header, question, options, multiSelect }
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

    // ── replayComplete ────────────────────────────────────────────────────────
    case 'replayComplete': {
      return { ...state, replaying: false }
    }

    // ── error ─────────────────────────────────────────────────────────────────
    case 'error': {
      return { ...state, error: { code: ev.code, message: ev.message, resetsAt: ev.resetsAt } }
    }

    // ── queueUpdate ───────────────────────────────────────────────────────────
    case 'queueUpdate': {
      return { ...state, queued: ev.items }
    }

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

    default:
      return state
  }
}
