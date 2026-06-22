import type { SessionEvent, ToolKind } from './events'

// ── Block types ───────────────────────────────────────────────────────────────

export type Block =
  | { kind: 'text';       id: string; text: string }
  | { kind: 'thinking';   id: string; text: string }
  | { kind: 'tool';       id: string; toolId: string; name: string; toolKind: ToolKind; input: Record<string, unknown>; result?: { ok: boolean; payload: Record<string, unknown> } }
  | { kind: 'permission'; id: string; permId: string; tool: string; toolKind: ToolKind; input: Record<string, unknown>; plan: boolean }
  | { kind: 'question';   id: string; questionId: string; header: string; question: string; options: Array<Record<string, unknown>>; multiSelect: boolean; live?: boolean }
  | { kind: 'error';      id: string; code: string; message: string; resetsAt?: string | null }

// ── Turn ─────────────────────────────────────────────────────────────────────

export interface Turn { id: string; role: 'user' | 'assistant'; blocks: Block[] }

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
  replaying: boolean            // true until replayComplete
  error: { code: string; message: string; resetsAt?: string | null } | null
  queued: string[]              // texts of prompts waiting in the server-side queue
  rotations: RotationMarker[]   // account rotations, anchored into the timeline
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
  rotations: [],
}

/** Coerce a possibly-missing numeric usage field to a finite number (0 when absent). */
function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

// ── Local (UI-only) synthetic actions ────────────────────────────────────────

export type LocalAction =
  | { type: '_resolvePermission'; permId: string }
  | { type: '_resolveQuestion';   questionId: string }
  | { type: '_rotation';          from: string; to: string; reason?: string }

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

// ── Reducer ───────────────────────────────────────────────────────────────────

export function transcriptReducer(
  state: TranscriptState,
  ev: SessionEvent | LocalAction,
): TranscriptState {
  switch (ev.type) {
    // ── user ─────────────────────────────────────────────────────────────────
    case 'user': {
      const { turnId, text } = ev
      const userBlock: Block = { kind: 'text', id: `${turnId}:u`, text }
      const idx = findTurnIdx(state.turns, turnId)
      let nextTurns: Turn[]
      if (idx === -1) {
        // Append new user turn
        nextTurns = [...state.turns, { id: turnId, role: 'user', blocks: [userBlock] }]
      } else {
        // Upsert: replace the text block in the existing turn
        nextTurns = replaceTurn(state.turns, idx, (t) => ({
          ...t,
          blocks: t.blocks.map((b) => (b.kind === 'text' && b.id === `${turnId}:u` ? userBlock : b)),
        }))
      }
      return { ...state, turns: nextTurns, openBlockId: null }
    }

    // ── turnStart ─────────────────────────────────────────────────────────────
    case 'turnStart': {
      const { turnId } = ev
      const idx = findTurnIdx(state.turns, turnId)
      if (idx !== -1) {
        // Idempotent — turn already exists. If openBlockId is already null, return state unchanged.
        if (state.openBlockId === null) return state
        return { ...state, openBlockId: null }
      }
      return {
        ...state,
        turns: [...state.turns, { id: turnId, role: 'assistant', blocks: [] }],
        openBlockId: null,
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
        const newBlock: Block = { kind: 'text', id: blockId, text }
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
        const newBlock: Block = { kind: 'thinking', id: blockId, text }
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

      const newBlock: Block = { kind: 'tool', id: toolId, toolId, name, toolKind, input }
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
          i === blockIdx && b.kind === 'tool' ? { ...b, result: { ok, payload } } : b,
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

      const newBlock: Block = { kind: 'question', id: questionId, questionId, header, question, options, multiSelect, live: !state.replaying }
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
        code: ev.code, message: ev.message, resetsAt: ev.resetsAt,
      }
      const turns = replaceTurn(withTurn, idx, (t) => ({ ...t, blocks: [...t.blocks, errBlock] }))
      return { ...state, turns, error: banner }
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

    default:
      return state
  }
}
