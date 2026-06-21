import { describe, it, expect } from 'vitest'
import { selectSubAgents } from './selectors'
import type { Turn } from './reducer'

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
