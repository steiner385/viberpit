import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from '../reducer'

describe('reducer sub-agent attribution', () => {
  it('tags turns with agentId from the event', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'm1', agentId: null })
    s = transcriptReducer(s, { type: 'turnStart', turnId: 's1', agentId: 'toolu_A' })
    expect(s.turns.find((t) => t.id === 'm1')?.agentId ?? null).toBe(null)
    expect(s.turns.find((t) => t.id === 's1')?.agentId).toBe('toolu_A')
  })

  it('folds subAgentUpdate into the subAgents map (latest status wins)', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'subAgentUpdate', agentId: 'toolu_A', parentAgentId: null,
                               agentType: 'Explore', description: 'd', status: 'running' })
    s = transcriptReducer(s, { type: 'subAgentUpdate', agentId: 'toolu_A', parentAgentId: null,
                               agentType: 'Explore', description: 'd', status: 'done' })
    expect(s.subAgents['toolu_A'].status).toBe('done')
  })
})
