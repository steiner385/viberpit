/**
 * Tests for transcriptReducer (session/reducer.ts).
 * Pure function — no mocks needed.
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { TranscriptState } from './reducer'
import type { SessionEvent } from './events'

// ── Helpers ───────────────────────────────────────────────────────────────────

function reduce(events: (SessionEvent | Parameters<typeof transcriptReducer>[1])[], initial = initialTranscript): TranscriptState {
  return events.reduce((s, ev) => transcriptReducer(s, ev as Parameters<typeof transcriptReducer>[1]), initial)
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('transcriptReducer', () => {
  let base: TranscriptState

  beforeEach(() => {
    base = { ...initialTranscript, turns: [], openBlockId: null }
  })

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

  it('orphan toolResult (no matching toolCall) does not throw and leaves state unchanged', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
    ])
    const next = transcriptReducer(s, { type: 'toolResult', toolId: 'nonexistent', ok: true, payload: {} })
    expect(next).toBe(s) // referentially same — no change
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

  it('_resolveQuestion removes the matching question block', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'question',  questionId: 'q-1', turnId: 't1', header: 'Pick one', question: 'Which?', options: [], multiSelect: false },
    ])
    expect(s.turns[0].blocks.some((b) => b.kind === 'question')).toBe(true)

    const resolved = transcriptReducer(s, { type: '_resolveQuestion', questionId: 'q-1' })
    expect(resolved.turns[0].blocks.some((b) => b.kind === 'question')).toBe(false)
  })

  // ── error ─────────────────────────────────────────────────────────────────

  it('error sets state.error', () => {
    const s = transcriptReducer(initialTranscript, { type: 'error', code: 'rate_limit', message: 'Too many requests', resetsAt: '2024-01-01T00:00:00Z' })
    expect(s.error).toEqual({ code: 'rate_limit', message: 'Too many requests', resetsAt: '2024-01-01T00:00:00Z' })
  })
})
