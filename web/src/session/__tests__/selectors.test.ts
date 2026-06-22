import { describe, it, expect } from 'vitest'
import { selectTurnsForAgent, selectSubAgentTree } from '../selectors'
import type { Turn } from '../reducer'

const turns: Turn[] = [
  { id: 'm1', role: 'assistant', blocks: [], agentId: null },
  { id: 's1', role: 'assistant', blocks: [], agentId: 'toolu_A' },
]

describe('selectors', () => {
  it('filters turns by agentId', () => {
    expect(selectTurnsForAgent(turns, null).map((t) => t.id)).toEqual(['m1'])
    expect(selectTurnsForAgent(turns, 'toolu_A').map((t) => t.id)).toEqual(['s1'])
  })

  it('builds a nested tree to arbitrary depth', () => {
    const map = {
      toolu_A: { agentId: 'toolu_A', parentAgentId: null, agentType: 'Explore', description: 'd', status: 'done' as const },
      toolu_B: { agentId: 'toolu_B', parentAgentId: 'toolu_A', agentType: 'agent', description: 'n', status: 'running' as const },
    }
    const tree = selectSubAgentTree(map)
    expect(tree).toHaveLength(1)
    expect(tree[0].agentId).toBe('toolu_A')
    expect(tree[0].children[0].agentId).toBe('toolu_B')
  })
})
