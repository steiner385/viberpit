// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { selectSubAgents, selectRunningSubAgentCount } from '../selectors'
import type { Turn, SubThread } from '../reducer'

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

describe('selectSubAgents merges live task state', () => {
  const threads: Record<string, SubThread> = {
    x1: { turns: [], openBlockId: null, task: { taskId: 'task1', description: 'scan', agentType: 'Explore', backgrounded: true, depth: 1, status: 'stopped', startedAt: 1000, endedAt: 3000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 1, summary: 'halted' } },
  }
  it('task status wins over the tool-result derivation', () => {
    const [a] = selectSubAgents([toolTurn('Task', false)], threads)
    expect(a.status).toBe('stopped')
    expect(a).toMatchObject({ taskId: 'task1', activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 1, summary: 'halted', backgrounded: true, endedAt: 3000 })
  })
  it('completed maps to done; a running task stays running even with no result', () => {
    const done = { x1: { ...threads.x1, task: { ...threads.x1.task!, status: 'completed' as const } } }
    expect(selectSubAgents([toolTurn('Task', false)], done)[0].status).toBe('done')
    const running = { x1: { ...threads.x1, task: { ...threads.x1.task!, status: 'running' as const, endedAt: undefined } } }
    expect(selectSubAgents([toolTurn('Task', true)], running)[0].status).toBe('running')
  })
  it('falls back to the tool result when the thread has no task (replayed session)', () => {
    expect(selectSubAgents([toolTurn('Task', true)], { x1: { turns: [], openBlockId: null } })[0].status).toBe('done')
    expect(selectSubAgents([toolTurn('Task', false)])[0].status).toBe('running')
  })
})
