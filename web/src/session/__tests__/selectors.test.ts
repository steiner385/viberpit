import { describe, it, expect } from 'vitest'
import { selectSubAgents, selectRunningSubAgentCount } from '../selectors'
import type { Turn } from '../reducer'

function toolTurn(name: string, withResult: boolean): Turn {
  return {
    id: 't1', role: 'assistant',
    blocks: [{
      kind: 'tool', id: 'b1', toolId: 'x1', name, toolKind: 'unknown',
      input: { subagent_type: 'Explore', description: 'scan' },
      ...(withResult ? { result: { ok: true, payload: {} } } : {}),
    }],
  }
}

describe('selectRunningSubAgentCount', () => {
  it('counts unresolved Task tool calls', () => {
    expect(selectRunningSubAgentCount([toolTurn('Task', false)])).toBe(1)
  })
  it('counts unresolved Agent-named tool calls too', () => {
    expect(selectRunningSubAgentCount([toolTurn('Agent', false)])).toBe(1)
  })
  it('does not count resolved sub-agents', () => {
    expect(selectRunningSubAgentCount([toolTurn('Task', true)])).toBe(0)
  })
})

describe('selectSubAgents Agent-name match', () => {
  it('includes Agent-named tool calls', () => {
    expect(selectSubAgents([toolTurn('Agent', false)])).toHaveLength(1)
  })
})
