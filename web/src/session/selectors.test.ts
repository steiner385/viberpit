// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { selectSubAgents, selectVisibleTurns, selectVisibleSubAgents } from './selectors'
import { initialTranscript } from './reducer'
import type { Turn, TranscriptState, SubThread } from './reducer'

function toolTurn(blocks: Turn['blocks']): Turn {
  return { id: 't1', role: 'assistant', blocks }
}

describe('selectSubAgents', () => {
  it('returns [] when there are no Task tool calls', () => {
    const turns: Turn[] = [
      toolTurn([
        { kind: 'tool', id: 'b1', toolId: 'x1', name: 'Bash', toolKind: 'bash', input: {} },
      ]),
    ]
    expect(selectSubAgents(turns)).toEqual([])
  })

  it('extracts type + description from a Task tool call', () => {
    const turns: Turn[] = [
      toolTurn([
        {
          kind: 'tool', id: 'b1', toolId: 'x1', name: 'Task', toolKind: 'unknown',
          input: { subagent_type: 'Explore', description: 'find the bug' },
        },
      ]),
    ]
    expect(selectSubAgents(turns)).toEqual([
      { toolId: 'x1', type: 'Explore', description: 'find the bug', status: 'running' },
    ])
  })

  it('marks a Task done when its result is ok', () => {
    const turns: Turn[] = [
      toolTurn([
        {
          kind: 'tool', id: 'b1', toolId: 'x1', name: 'Task', toolKind: 'unknown',
          input: { subagent_type: 'Plan', description: 'plan it' },
          result: { ok: true, payload: {} },
        },
      ]),
    ]
    expect(selectSubAgents(turns)[0].status).toBe('done')
  })

  it('marks a Task failed when its result is not ok', () => {
    const turns: Turn[] = [
      toolTurn([
        {
          kind: 'tool', id: 'b1', toolId: 'x1', name: 'Task', toolKind: 'unknown',
          input: { subagent_type: 'Plan', description: 'plan it' },
          result: { ok: false, payload: {} },
        },
      ]),
    ]
    expect(selectSubAgents(turns)[0].status).toBe('failed')
  })

  it('surfaces startedAt (block ts) and endedAt (result ts)', () => {
    const turns: Turn[] = [
      toolTurn([
        {
          kind: 'tool', id: 'b1', toolId: 'x1', name: 'Task', toolKind: 'task',
          input: { subagent_type: 'Explore', description: 'find it' }, ts: 1000,
          result: { ok: true, payload: {}, ts: 4000 },
        },
        {
          kind: 'tool', id: 'b2', toolId: 'x2', name: 'Task', toolKind: 'task',
          input: { subagent_type: 'Plan', description: 'still going' }, ts: 2000,
        },
      ]),
    ]
    const agents = selectSubAgents(turns)
    expect(agents[0]).toMatchObject({ status: 'done', startedAt: 1000, endedAt: 4000 })
    expect(agents[1]).toMatchObject({ status: 'running', startedAt: 2000 })
    expect(agents[1].endedAt).toBeUndefined()
  })

  it('falls back to "agent" / "" when subagent_type/description are missing', () => {
    const turns: Turn[] = [
      toolTurn([
        { kind: 'tool', id: 'b1', toolId: 'x1', name: 'Task', toolKind: 'unknown', input: {} },
      ]),
    ]
    expect(selectSubAgents(turns)).toEqual([
      { toolId: 'x1', type: 'agent', description: '', status: 'running' },
    ])
  })
})

// ── Visible-transcript double-buffer selectors ────────────────────────────────

describe('selectVisibleTurns / selectVisibleSubAgents', () => {
  const liveTurns: Turn[] = [{ id: 'live', role: 'assistant', blocks: [] }]
  const staleTurns: Turn[] = [{ id: 'stale', role: 'assistant', blocks: [] }]
  const liveSubs: Record<string, SubThread> = { a: { turns: [], openBlockId: null } }
  const staleSubs: Record<string, SubThread> = { b: { turns: [], openBlockId: null } }

  function make(over: Partial<TranscriptState>): TranscriptState {
    return { ...initialTranscript, ...over }
  }

  it('serves live content when not replaying', () => {
    const s = make({ replaying: false, turns: liveTurns, staleTurns, subAgents: liveSubs, staleSubAgents: staleSubs })
    expect(selectVisibleTurns(s)).toBe(liveTurns)
    expect(selectVisibleSubAgents(s)).toBe(liveSubs)
  })

  it('serves stale content while replaying and stale exists', () => {
    const s = make({ replaying: true, turns: liveTurns, staleTurns, subAgents: liveSubs, staleSubAgents: staleSubs })
    expect(selectVisibleTurns(s)).toBe(staleTurns)
    expect(selectVisibleSubAgents(s)).toBe(staleSubs)
  })

  it('falls through to live content while replaying when there is no stale (first attach)', () => {
    const s = make({ replaying: true, turns: liveTurns, staleTurns: [], subAgents: liveSubs, staleSubAgents: {} })
    expect(selectVisibleTurns(s)).toBe(liveTurns)
    expect(selectVisibleSubAgents(s)).toBe(liveSubs)
  })
})
