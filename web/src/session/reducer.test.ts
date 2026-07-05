/**
 * Tests for transcriptReducer (session/reducer.ts).
 * Pure function — no mocks needed.
 */

import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { TranscriptState } from './reducer'
import type { SessionEvent } from './events'

// ── Helpers ───────────────────────────────────────────────────────────────────

function reduce(events: (SessionEvent | Parameters<typeof transcriptReducer>[1])[], initial = initialTranscript): TranscriptState {
  return events.reduce((s, ev) => transcriptReducer(s, ev as Parameters<typeof transcriptReducer>[1]), initial)
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('transcriptReducer', () => {
  // ── textDelta appending ────────────────────────────────────────────────────

  it('appends text delta into open turn; second delta appends to same block', () => {
    const s1 = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'textDelta', turnId: 't1', text: 'Hello ' },
    ])
    expect(s1.turns).toHaveLength(1)
    expect(s1.turns[0].blocks).toHaveLength(1)
    const block1 = s1.turns[0].blocks[0]
    expect(block1.kind).toBe('text')
    if (block1.kind === 'text') expect(block1.text).toBe('Hello ')
    expect(s1.openBlockId).toBe(block1.id)

    const s2 = transcriptReducer(s1, { type: 'textDelta', turnId: 't1', text: 'World' })
    expect(s2.turns[0].blocks).toHaveLength(1)
    const block2 = s2.turns[0].blocks[0]
    if (block2.kind === 'text') expect(block2.text).toBe('Hello World')
    expect(s2.openBlockId).toBe(block2.id)
  })

  // ── toolCall mid-text closes the text block, new textDelta opens a new block

  it('toolCall mid-text closes text block; following textDelta opens a NEW text block', () => {
    const s = reduce([
      { type: 'turnStart',  turnId: 't1' },
      { type: 'textDelta',  turnId: 't1', text: 'Before tool' },
      { type: 'toolCall',   turnId: 't1', toolId: 'tool-1', name: 'Bash', toolKind: 'bash', input: {} },
      { type: 'textDelta',  turnId: 't1', text: 'After tool' },
    ])
    const blocks = s.turns[0].blocks
    expect(blocks).toHaveLength(3)
    expect(blocks[0].kind).toBe('text')
    expect(blocks[1].kind).toBe('tool')
    expect(blocks[2].kind).toBe('text')
    if (blocks[0].kind === 'text') expect(blocks[0].text).toBe('Before tool')
    if (blocks[2].kind === 'text') expect(blocks[2].text).toBe('After tool')
    // The two text blocks have different ids
    expect(blocks[0].id).not.toBe(blocks[2].id)
  })

  // ── orphan toolResult ──────────────────────────────────────────────────────

  it('orphan toolResult (no matching toolCall) does not throw, leaves rendering unchanged, and is parked', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
    ])
    const next = transcriptReducer(s, { type: 'toolResult', toolId: 'nonexistent', ok: true, payload: {} })
    // Nothing renders differently — same turns/openBlockId reference — but the
    // result is parked (not dropped) for a toolCall that may still arrive.
    expect(next.turns).toBe(s.turns)
    expect(next.openBlockId).toBe(s.openBlockId)
    expect(next.orphanToolResults['nonexistent']).toEqual({ ok: true, payload: {} })
  })

  it('a late toolCall adopts an earlier orphaned toolResult (main thread)', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'toolResult', toolId: 'tool-1', ok: true, payload: { content: 'late result' } },
    ])
    // Parked, not yet visible on any block.
    expect(s.orphanToolResults['tool-1']).toEqual({ ok: true, payload: { content: 'late result' } })

    const next = transcriptReducer(s, {
      type: 'toolCall', turnId: 't1', toolId: 'tool-1', name: 'Read', toolKind: 'read', input: { path: '/foo' },
    })
    const toolBlock = next.turns[0].blocks.find((b) => b.kind === 'tool')
    expect(toolBlock).toMatchObject({ kind: 'tool', toolId: 'tool-1', result: { ok: true, payload: { content: 'late result' } } })
    // Consumed — no longer sitting in the buffer.
    expect(next.orphanToolResults['tool-1']).toBeUndefined()
  })

  it('caps the orphan toolResult buffer, evicting the oldest entry first', () => {
    const events: SessionEvent[] = [{ type: 'turnStart', turnId: 't1' }]
    // One more than the cap (50) — the first inserted ("orphan-0") must be evicted.
    for (let i = 0; i < 51; i++) {
      events.push({ type: 'toolResult', toolId: `orphan-${i}`, ok: true, payload: { i } })
    }
    const s = reduce(events)
    expect(Object.keys(s.orphanToolResults)).toHaveLength(50)
    expect(s.orphanToolResults['orphan-0']).toBeUndefined()
    expect(s.orphanToolResults['orphan-50']).toEqual({ ok: true, payload: { i: 50 } })
  })

  // ── toolResult attaches to matching tool block ─────────────────────────────

  it('toolResult attaches to the matching tool block by toolId', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'toolCall',  turnId: 't1', toolId: 'tool-1', name: 'Read', toolKind: 'read', input: { path: '/foo' } },
      { type: 'toolResult', toolId: 'tool-1', ok: true, payload: { content: 'file contents' } },
    ])
    const toolBlock = s.turns[0].blocks[0]
    expect(toolBlock.kind).toBe('tool')
    if (toolBlock.kind === 'tool') {
      expect(toolBlock.result).toEqual({ ok: true, payload: { content: 'file contents' } })
    }
  })

  // ── permissionRequest + turnEnd keeps block; _resolvePermission removes it ─

  it('permissionRequest then turnEnd keeps the permission block; _resolvePermission removes it', () => {
    const s = reduce([
      { type: 'turnStart',         turnId: 't1' },
      { type: 'permissionRequest', permId: 'perm-1', turnId: 't1', tool: 'Bash', toolKind: 'bash', input: { cmd: 'ls' }, plan: false },
      { type: 'turnEnd',           turnId: 't1' },
    ])
    const permBlock = s.turns[0].blocks.find((b) => b.kind === 'permission')
    expect(permBlock).toBeDefined()

    const resolved = transcriptReducer(s, { type: '_resolvePermission', permId: 'perm-1' })
    const remaining = resolved.turns[0].blocks.filter((b) => b.kind === 'permission')
    expect(remaining).toHaveLength(0)
  })

  // ── duplicate event idempotency ───────────────────────────────────────────

  it('feeding the same toolCall twice results in one tool block', () => {
    const toolCall: SessionEvent = { type: 'toolCall', turnId: 't1', toolId: 'tool-x', name: 'Edit', toolKind: 'edit', input: {} }
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      toolCall,
      toolCall,
    ])
    const toolBlocks = s.turns[0].blocks.filter((b) => b.kind === 'tool')
    expect(toolBlocks).toHaveLength(1)
  })

  it('feeding the same turnStart twice results in one turn', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'turnStart', turnId: 't1' },
    ])
    expect(s.turns).toHaveLength(1)
  })

  // ── status, replayComplete, title ──────────────────────────────────────────

  it('status updates state.status as {state,count}', () => {
    const s = transcriptReducer(initialTranscript, { type: 'status', state: 'running', count: 3 })
    expect(s.status).toEqual({ state: 'running', count: 3 })
  })

  it('replayComplete flips replaying to false', () => {
    expect(initialTranscript.replaying).toBe(true)
    const s = transcriptReducer(initialTranscript, { type: 'replayComplete' })
    expect(s.replaying).toBe(false)
  })

  it('title sets state.title', () => {
    const s = transcriptReducer(initialTranscript, { type: 'title', title: 'My Session', source: 'auto' })
    expect(s.title).toBe('My Session')
  })

  // ── replayReset / reconnect re-replay ──────────────────────────────────────

  it('re-replaying the same transcript on reconnect does NOT duplicate content', () => {
    // A small completed turn as replay would emit it (one coalesced textDelta
    // carrying the full block text, plus a thinking block).
    const replay: SessionEvent[] = [
      { type: 'turnStart',     turnId: 't1' },
      { type: 'thinkingDelta', turnId: 't1', text: 'Let me think.' },
      { type: 'textDelta',     turnId: 't1', text: 'Hello world' },
      { type: 'turnEnd',       turnId: 't1' },
      { type: 'replayComplete' },
    ]

    const first = reduce(replay)
    expect(first.turns).toHaveLength(1)
    expect(first.turns[0].blocks).toHaveLength(2) // thinking + text

    // Reconnect: the server prepends a replayReset, then replays the identical
    // transcript. The reducer must rebuild — not append a second copy.
    const reconnect = reduce(
      [{ type: 'replayReset' } as SessionEvent, ...replay],
      first,
    )
    expect(reconnect.turns).toHaveLength(1)
    expect(reconnect.turns[0].blocks).toHaveLength(2)
    const text = reconnect.turns[0].blocks.find((b) => b.kind === 'text')
    expect(text && text.kind === 'text' && text.text).toBe('Hello world')
    const thinking = reconnect.turns[0].blocks.find((b) => b.kind === 'thinking')
    expect(thinking && thinking.kind === 'thinking' && thinking.text).toBe('Let me think.')
  })

  it('replayReset clears turns and re-enters replaying, preserving rotations', () => {
    const built = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'textDelta', turnId: 't1', text: 'hi' },
      { type: 'replayComplete' },
      { type: '_rotation', from: 'a', to: 'b' },
    ])
    expect(built.turns).toHaveLength(1)
    expect(built.replaying).toBe(false)
    expect(built.rotations).toHaveLength(1)

    const reset = transcriptReducer(built, { type: 'replayReset' } as SessionEvent)
    expect(reset.turns).toHaveLength(0)
    expect(reset.openBlockId).toBeNull()
    expect(reset.replaying).toBe(true)
    // Rotations are live-only (never re-emitted on reattach) — must survive reset.
    expect(reset.rotations).toHaveLength(1)
  })

  it('replayReset clears parked orphan toolResults so a post-reset toolCall does not adopt a stale one', () => {
    const built = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'toolResult', toolId: 'tool-1', ok: true, payload: { content: 'pre-reset' } },
    ])
    expect(built.orphanToolResults['tool-1']).toEqual({ ok: true, payload: { content: 'pre-reset' } })

    const reset = transcriptReducer(built, { type: 'replayReset' } as SessionEvent)
    expect(reset.orphanToolResults).toEqual({})

    // Fresh replay after the reset: toolCall for the same toolId, no toolResult
    // re-delivered yet — must NOT adopt the stale pre-reset payload.
    const after = transcriptReducer(reset, {
      type: 'toolCall', turnId: 't1', toolId: 'tool-1', name: 'Read', toolKind: 'read', input: {},
    })
    const toolBlock = after.turns.find((t) => t.id === 't1')?.blocks.find((b) => b.kind === 'tool')
    expect(toolBlock).toMatchObject({ kind: 'tool', toolId: 'tool-1' })
    if (toolBlock?.kind === 'tool') expect(toolBlock.result).toBeUndefined()
  })

  // ── referential stability of untouched turns ───────────────────────────────

  it('prior turns are referentially stable after a delta to the latest turn', () => {
    const prev = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'textDelta', turnId: 't1', text: 'First turn text' },
      { type: 'turnEnd',   turnId: 't1' },
      { type: 'turnStart', turnId: 't2' },
    ])
    expect(prev.turns).toHaveLength(2)

    const next = transcriptReducer(prev, { type: 'textDelta', turnId: 't2', text: 'Second turn' })
    expect(next.turns).toHaveLength(2)
    // The first turn object must be the same reference
    expect(next.turns[0]).toBe(prev.turns[0])
  })

  // ── thinkingDelta / cross-kind block switching ─────────────────────────────

  it('thinkingDelta after textDelta opens a new thinking block (different openBlockId)', () => {
    const s = reduce([
      { type: 'turnStart',     turnId: 't1' },
      { type: 'textDelta',     turnId: 't1', text: 'text' },
      { type: 'thinkingDelta', turnId: 't1', text: 'think' },
    ])
    const blocks = s.turns[0].blocks
    expect(blocks).toHaveLength(2)
    expect(blocks[0].kind).toBe('text')
    expect(blocks[1].kind).toBe('thinking')
    expect(s.openBlockId).toBe(blocks[1].id)
  })

  it('consecutive thinkingDeltas append to the same thinking block', () => {
    const s = reduce([
      { type: 'turnStart',     turnId: 't1' },
      { type: 'thinkingDelta', turnId: 't1', text: 'part1 ' },
      { type: 'thinkingDelta', turnId: 't1', text: 'part2' },
    ])
    const blocks = s.turns[0].blocks
    expect(blocks).toHaveLength(1)
    if (blocks[0].kind === 'thinking') expect(blocks[0].text).toBe('part1 part2')
  })

  // ── question ──────────────────────────────────────────────────────────────

  it('_resolveQuestion marks the question answered in place (kept inline, not live, no tail turn)', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'question',  questionId: 'q-1', turnId: 't1', header: 'Pick one', question: 'Which?', options: [{ label: 'A' }, { label: 'B' }], multiSelect: false },
    ])
    const turnCount = s.turns.length

    const resolved = transcriptReducer(s, { type: '_resolveQuestion', questionId: 'q-1', selected: ['A'] })
    // The block stays at its original position in the asking turn — not removed,
    // and crucially NOT appended as a separate user turn at the transcript tail.
    expect(resolved.turns).toHaveLength(turnCount)
    const q = resolved.turns[0].blocks.find((b) => b.kind === 'question')
    expect(q && q.kind === 'question' && q.answered).toEqual(['A'])
    expect(q && q.kind === 'question' && q.live).toBe(false)
    expect(resolved.turns.some((t) => t.role === 'user')).toBe(false)
  })

  // ── error ─────────────────────────────────────────────────────────────────

  it('error sets state.error', () => {
    const s = transcriptReducer(initialTranscript, { type: 'error', code: 'rate_limit', message: 'Too many requests', resetsAt: '2024-01-01T00:00:00Z' })
    expect(s.error).toEqual({ code: 'rate_limit', message: 'Too many requests', resetsAt: '2024-01-01T00:00:00Z' })
  })

  it('error without turnId sets only the banner, no transcript block', () => {
    const s = transcriptReducer(initialTranscript, { type: 'error', code: 'INVALID_JSON', message: 'bad frame' })
    expect(s.error?.code).toBe('INVALID_JSON')
    expect(s.turns).toHaveLength(0)
  })

  it('_clearError clears a surfaced error', () => {
    const s = transcriptReducer(initialTranscript, { type: 'error', code: 'answer_not_sent', message: 'nope' })
    expect(s.error).not.toBeNull()
    const cleared = transcriptReducer(s, { type: '_clearError' })
    expect(cleared.error).toBeNull()
  })

  it('_clearError with no error returns the same state reference', () => {
    expect(transcriptReducer(initialTranscript, { type: '_clearError' })).toBe(initialTranscript)
  })

  it('error with turnId appends an inline error block to that turn AND sets the banner', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'textDelta', turnId: 't1', text: 'working...' },
      { type: 'error', code: 'max_turns', message: 'Reached the maximum number of turns', turnId: 't1' },
    ])
    // banner
    expect(s.error).toEqual({ code: 'max_turns', message: 'Reached the maximum number of turns', resetsAt: undefined })
    // inline block on the failing turn
    const turn = s.turns.find((t) => t.id === 't1')!
    const errBlock = turn.blocks.find((b) => b.kind === 'error')!
    expect(errBlock).toBeDefined()
    if (errBlock.kind === 'error') {
      expect(errBlock.code).toBe('max_turns')
      expect(errBlock.message).toBe('Reached the maximum number of turns')
    }
  })

  it('two errors on the same turn keep both inline blocks (timeline is not overwritten)', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'error', code: 'api_429', message: 'rate limited', turnId: 't1' },
      { type: 'error', code: 'execution_error', message: 'boom', turnId: 't1' },
    ])
    const turn = s.turns.find((t) => t.id === 't1')!
    const errs = turn.blocks.filter((b) => b.kind === 'error')
    expect(errs).toHaveLength(2)
    // banner reflects the latest
    expect(s.error?.code).toBe('execution_error')
  })

  it('error with turnId for an unseen turn creates the turn with the error block', () => {
    const s = transcriptReducer(initialTranscript, { type: 'error', code: 'agent_error', message: 'oops', turnId: 'tX' })
    const turn = s.turns.find((t) => t.id === 'tX')!
    expect(turn).toBeDefined()
    expect(turn.blocks.some((b) => b.kind === 'error')).toBe(true)
  })

  // ── Orphan textDelta before turnStart ──────────────────────────────────────

  it('textDelta arriving BEFORE any turnStart creates the assistant turn and opens a text block', () => {
    const s = transcriptReducer(initialTranscript, { type: 'textDelta', turnId: 't1', text: 'orphan text' })
    expect(s.turns).toHaveLength(1)
    expect(s.turns[0].id).toBe('t1')
    expect(s.turns[0].role).toBe('assistant')
    expect(s.turns[0].blocks).toHaveLength(1)
    const textBlock = s.turns[0].blocks[0]
    expect(textBlock.kind).toBe('text')
    if (textBlock.kind === 'text') expect(textBlock.text).toBe('orphan text')
    expect(s.openBlockId).toBe(textBlock.id)
  })

  // ── Duplicate permissionRequest idempotency ───────────────────────────────

  it('duplicate permissionRequest with the same permId results in only one permission block', () => {
    const req: SessionEvent = { type: 'permissionRequest', permId: 'perm-1', turnId: 't1', tool: 'Bash', toolKind: 'bash', input: { cmd: 'ls' }, plan: false }
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      req,
      req,
    ])
    const permBlocks = s.turns[0].blocks.filter((b) => b.kind === 'permission')
    expect(permBlocks).toHaveLength(1)
  })

  // ── Duplicate question idempotency ─────────────────────────────────────────

  it('duplicate question with the same questionId results in only one question block', () => {
    const q: SessionEvent = { type: 'question', questionId: 'q-1', turnId: 't1', header: 'Pick', question: 'Which?', options: [], multiSelect: false }
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      q,
      q,
    ])
    const questionBlocks = s.turns[0].blocks.filter((b) => b.kind === 'question')
    expect(questionBlocks).toHaveLength(1)
  })

  it('re-emitted question after replayComplete upgrades a replay-created block to live', () => {
    // Reproduces the refresh/reconnect bug: during replay (replaying=true) the
    // question is created live:false; the server then re-emits the still-pending
    // question after replayComplete. The idempotency guard must upgrade the
    // existing block to live:true instead of dropping the re-emit.
    const q: SessionEvent = { type: 'question', questionId: 'q-1', turnId: 't1', header: 'Pick', question: 'Which?', options: [], multiSelect: false }
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      q,                            // replay: block created with live:false
      { type: 'replayComplete' },   // replaying → false
      q,                            // re-emit of the still-pending question
    ])
    const blocks = s.turns[0].blocks.filter((b) => b.kind === 'question')
    expect(blocks).toHaveLength(1) // still idempotent — exactly one block
    expect(blocks.every((b) => b.kind === 'question' && b.live)).toBe(true)
  })

  // ── Referential stability on no-ops ────────────────────────────────────────

  it('_resolvePermission for an unknown permId returns the same state reference', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'permissionRequest', permId: 'perm-1', turnId: 't1', tool: 'Bash', toolKind: 'bash', input: { cmd: 'ls' }, plan: false },
    ])
    const next = transcriptReducer(s, { type: '_resolvePermission', permId: 'unknown-perm' })
    expect(next).toBe(s)
  })

  it('_resolveQuestion for an unknown questionId returns the same state reference', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'question', questionId: 'q-1', turnId: 't1', header: 'Pick', question: 'Which?', options: [], multiSelect: false },
    ])
    const next = transcriptReducer(s, { type: '_resolveQuestion', questionId: 'unknown-q' })
    expect(next).toBe(s)
  })

  it('duplicate turnStart when openBlockId is already null returns the same state reference', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'textDelta', turnId: 't1', text: 'hello' },
      { type: 'turnEnd', turnId: 't1' }, // openBlockId becomes null
    ])
    // Dispatch a duplicate turnStart for the same turn with openBlockId already null
    const next = transcriptReducer(s, { type: 'turnStart', turnId: 't1' })
    expect(next).toBe(s)
  })

  it('duplicate toolCall when turn already exists and openBlockId is already null returns the same state reference', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'toolCall', turnId: 't1', toolId: 'tool-x', name: 'Edit', toolKind: 'edit', input: {} },
      { type: 'turnEnd', turnId: 't1' }, // openBlockId becomes null
    ])
    // Dispatch a duplicate toolCall for the same tool with openBlockId already null
    const next = transcriptReducer(s, { type: 'toolCall', turnId: 't1', toolId: 'tool-x', name: 'Edit', toolKind: 'edit', input: {} })
    expect(next).toBe(s)
  })

  // ── recovering ───────────────────────────────────────────────────────────

  it('recovering event appends a soft block and does NOT raise the error banner', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' } as SessionEvent,
      { type: 'recovering', turnId: 't1', message: 'Recovering the dropped turn…' } as SessionEvent,
    ])
    const turn = s.turns.find(t => t.id === 't1')!
    expect(turn.blocks.some(b => b.kind === 'recovering')).toBe(true)
    expect(s.error).toBeNull()
  })

  // ── queueUpdate ────────────────────────────────────────────────────────────

  it('queueUpdate sets state.queued to the provided items', () => {
    const s = transcriptReducer(initialTranscript, { type: 'queueUpdate', items: [{ text: 'prompt one', images: [] }, { text: 'prompt two', images: [] }] })
    expect(s.queued.map(i => i.text)).toEqual(['prompt one', 'prompt two'])
  })

  it('queueUpdate with empty array clears state.queued', () => {
    const withQueued = transcriptReducer(initialTranscript, { type: 'queueUpdate', items: [{ text: 'x', images: [] }, { text: 'y', images: [] }] })
    const cleared = transcriptReducer(withQueued, { type: 'queueUpdate', items: [] })
    expect(cleared.queued).toEqual([])
  })

  it('initialTranscript has queued as empty array', () => {
    expect(initialTranscript.queued).toEqual([])
  })

  // ── rotations (interleaved into the timeline) ───────────────────────────────

  it('initialTranscript has rotations as empty array', () => {
    expect(initialTranscript.rotations).toEqual([])
  })

  it('anchors a rotation to the last turn present when it arrives', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'textDelta', turnId: 't1', text: 'hi' },
      { type: 'turnEnd', turnId: 't1' },
      { type: '_rotation', from: 'alice', to: 'bob', reason: 'rate limited' },
    ])
    expect(s.rotations).toEqual([
      { from: 'alice', to: 'bob', reason: 'rate limited', afterTurnId: 't1' },
    ])
  })

  it('anchors a rotation that arrives before any turn to null (renders at top)', () => {
    const s = reduce([{ type: '_rotation', from: 'alice', to: 'bob' }])
    expect(s.rotations).toEqual([
      { from: 'alice', to: 'bob', reason: undefined, afterTurnId: null },
    ])
  })

  it('keeps rotations in arrival order, each anchored to the then-current last turn', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: '_rotation', from: 'a', to: 'b' },
      { type: 'turnStart', turnId: 't2' },
      { type: '_rotation', from: 'b', to: 'c' },
    ])
    expect(s.rotations.map((r) => [r.from, r.to, r.afterTurnId])).toEqual([
      ['a', 'b', 't1'],
      ['b', 'c', 't2'],
    ])
  })

  it('does not touch turns when a rotation arrives', () => {
    const withTurn = reduce([{ type: 'turnStart', turnId: 't1' }])
    const after = transcriptReducer(withTurn, { type: '_rotation', from: 'a', to: 'b' })
    expect(after.turns).toBe(withTurn.turns) // referentially stable
  })

  // ── latestSuggestions ────────────────────────────────────────────────────

  it('stores suggestions from a suggestions event', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'suggestions', sessionId: 's1', turnId: 't1', items: ['Run tests', 'Commit'],
    })
    expect(s.latestSuggestions).toEqual(['Run tests', 'Commit'])
  })

  it('clears suggestions on the next turnStart', () => {
    const s1 = transcriptReducer(initialTranscript, {
      type: 'suggestions', sessionId: 's1', turnId: 't1', items: ['A'],
    })
    const s2 = transcriptReducer(s1, { type: 'turnStart', turnId: 't2' })
    expect(s2.latestSuggestions).toEqual([])
  })

  it('clears suggestions when the user sends a prompt', () => {
    const s1 = transcriptReducer(initialTranscript, {
      type: 'suggestions', sessionId: 's1', turnId: 't1', items: ['A'],
    })
    const s2 = transcriptReducer(s1, { type: 'user', turnId: 'u1', text: 'next thing' })
    expect(s2.latestSuggestions).toEqual([])
  })

  // ── question live flag ─────────────────────────────────────────────────────

  describe('question live flag', () => {
    const qEvent = { type: 'question', questionId: 'q1', turnId: 't1', header: 'H', question: 'Q?', options: [{ label: 'A' }], multiSelect: false } as const

    it('marks a question received during replay as not live', () => {
      // Precondition: the initial transcript must be in replaying mode
      expect(initialTranscript.replaying).toBe(true)

      const s = transcriptReducer(initialTranscript, qEvent)
      const block = s.turns.flatMap((t) => t.blocks).find((b) => b.kind === 'question')
      expect(block).toMatchObject({ kind: 'question', live: false })
    })

    it('marks a question received after replayComplete as live', () => {
      // Precondition: the initial transcript must be in replaying mode
      expect(initialTranscript.replaying).toBe(true)

      const afterReplay = transcriptReducer(initialTranscript, { type: 'replayComplete' } as const)
      const s = transcriptReducer(afterReplay, qEvent)
      const block = s.turns.flatMap((t) => t.blocks).find((b) => b.kind === 'question')
      expect(block).toMatchObject({ kind: 'question', live: true })
    })
  })

  // ── image blocks on user turns (attachments) ───────────────────────────────

  it('adds an image block carrying the wire images on an appended user turn', () => {
    const ev = {
      type: 'user', turnId: 'T1', text: 'look',
      images: [{ url: '/api/attachments/a1', mime: 'image/png', name: 'a.png' }],
    } as unknown as SessionEvent
    const s = transcriptReducer(initialTranscript, ev)
    const turn = s.turns.find((t) => t.id === 'T1')!
    expect(turn.blocks.some((b) => b.kind === 'text')).toBe(true)
    const img = turn.blocks.find((b) => b.kind === 'image') as
      | { kind: 'image'; images: Array<{ url: string; mime: string; name: string }> } | undefined
    expect(img?.images).toHaveLength(1)
    expect(img?.images[0]).toMatchObject({ url: '/api/attachments/a1', mime: 'image/png', name: 'a.png' })
  })

  it('carries images when a brand-new session adopts its optimistic seed turn', () => {
    let s = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'look' })
    s = transcriptReducer(s, {
      type: 'user', turnId: 'real-1', text: 'look',
      images: [{ url: '/api/attachments/a1', mime: 'image/png', name: 'a.png' }],
    } as unknown as SessionEvent)
    const turn = s.turns.find((t) => t.id === 'real-1')!
    expect(turn.blocks.find((b) => b.kind === 'image')).toBeDefined()
  })

  it('adds no image block when the user event carries no images', () => {
    const s = transcriptReducer(initialTranscript, { type: 'user', turnId: 'T2', text: 'hi' })
    const turn = s.turns.find((t) => t.id === 'T2')!
    expect(turn.blocks.some((b) => b.kind === 'image')).toBe(false)
  })
})

describe('sub-agent thread routing', () => {
  it('routes a tagged toolCall into subAgents, leaving main turns empty', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'sa1', parentToolUseId: 'task1' })
    s = transcriptReducer(s, {
      type: 'toolCall', turnId: 'sa1', toolId: 'g1', name: 'Grep', toolKind: 'search',
      input: {}, parentToolUseId: 'task1',
    })
    expect(s.turns).toEqual([])
    expect(s.subAgents['task1'].turns[0].blocks[0]).toMatchObject({ kind: 'tool', toolId: 'g1' })
  })

  it('keeps untagged events on the main thread', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 't1' })
    s = transcriptReducer(s, { type: 'textDelta', turnId: 't1', text: 'hi' })
    expect(s.turns[0].blocks[0]).toMatchObject({ kind: 'text', text: 'hi' })
    expect(s.subAgents).toEqual({})
  })

  it('isolates open-text state per thread during concurrent streaming', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 't1' })
    s = transcriptReducer(s, { type: 'textDelta', turnId: 't1', text: 'main-' })
    s = transcriptReducer(s, { type: 'textDelta', turnId: 'sa1', text: 'sub', parentToolUseId: 'task1' })
    s = transcriptReducer(s, { type: 'textDelta', turnId: 't1', text: 'continued' })
    const mainText = s.turns[0].blocks
      .filter((b) => b.kind === 'text').map((b) => (b as { text: string }).text).join('')
    expect(mainText).toBe('main-continued')
    expect(s.subAgents['task1'].turns[0].blocks[0]).toMatchObject({ kind: 'text', text: 'sub' })
  })

  it('routes a tagged toolResult into the matching sub-thread tool block', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'sa1', parentToolUseId: 'task1' })
    s = transcriptReducer(s, {
      type: 'toolCall', turnId: 'sa1', toolId: 'g1', name: 'Grep', toolKind: 'search',
      input: {}, parentToolUseId: 'task1',
    })
    s = transcriptReducer(s, {
      type: 'toolResult', toolId: 'g1', ok: true, payload: { content: 'hit' }, parentToolUseId: 'task1',
    })
    const block = s.subAgents['task1'].turns[0].blocks[0]
    expect(block).toMatchObject({ kind: 'tool', toolId: 'g1', result: { ok: true } })
  })

  it('a late tagged toolCall adopts an earlier orphaned toolResult inside its sub-thread', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'sa1', parentToolUseId: 'task1' })
    // The toolResult arrives first (reorder around a reconnect/heal boundary) —
    // no block exists in the sub-thread yet, so it must be parked, not dropped.
    s = transcriptReducer(s, {
      type: 'toolResult', toolId: 'g1', ok: true, payload: { content: 'hit' }, parentToolUseId: 'task1',
    })
    // turnStart already created the empty turn; the result must not appear on any block yet.
    expect(s.subAgents['task1'].turns[0].blocks).toEqual([])
    expect(s.orphanToolResults['g1']).toEqual({ ok: true, payload: { content: 'hit' } })

    s = transcriptReducer(s, {
      type: 'toolCall', turnId: 'sa1', toolId: 'g1', name: 'Grep', toolKind: 'search',
      input: {}, parentToolUseId: 'task1',
    })
    const block = s.subAgents['task1'].turns[0].blocks[0]
    expect(block).toMatchObject({ kind: 'tool', toolId: 'g1', result: { ok: true, payload: { content: 'hit' } } })
    expect(s.orphanToolResults['g1']).toBeUndefined()
  })
})

// ── Block timestamp (ts) tests ─────────────────────────────────────────────────

test('block ts is stamped at creation and not overwritten by streaming append', () => {
  let s = transcriptReducer(initialTranscript, { type: 'turnStart', turnId: 'a1' })
  s = transcriptReducer(s, { type: 'textDelta', turnId: 'a1', text: 'Hel', ts: 1000 })
  s = transcriptReducer(s, { type: 'textDelta', turnId: 'a1', text: 'lo', ts: 2000 })
  const block = s.turns.at(-1)!.blocks[0]
  expect(block.text).toBe('Hello')
  expect(block.ts).toBe(1000) // first delta wins
})

test('tool block keeps its create-time ts after toolResult', () => {
  let s = transcriptReducer(initialTranscript, { type: 'turnStart', turnId: 'a1' })
  s = transcriptReducer(s, { type: 'toolCall', turnId: 'a1', toolId: 'x', name: 'Bash', toolKind: 'bash', input: {}, ts: 500 })
  s = transcriptReducer(s, { type: 'toolResult', toolId: 'x', ok: true, payload: {}, ts: 999 })
  const block = s.turns.at(-1)!.blocks.find((b) => b.kind === 'tool')!
  expect(block.ts).toBe(500)
})

test('toolResult records its own ts on the block result (completion time)', () => {
  let s = transcriptReducer(initialTranscript, { type: 'turnStart', turnId: 'a1' })
  s = transcriptReducer(s, { type: 'toolCall', turnId: 'a1', toolId: 'x', name: 'Task', toolKind: 'task', input: {}, ts: 500 })
  s = transcriptReducer(s, { type: 'toolResult', toolId: 'x', ok: true, payload: {}, ts: 999 })
  const block = s.turns.at(-1)!.blocks.find((b) => b.kind === 'tool')!
  expect(block.kind === 'tool' && block.result).toEqual({ ok: true, payload: {}, ts: 999 })
})

test('user block carries ts', () => {
  const s = transcriptReducer(initialTranscript, { type: 'user', turnId: 'u1', text: 'hi', ts: 42 })
  expect(s.turns[0].blocks[0].ts).toBe(42)
})
