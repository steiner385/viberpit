import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from '../reducer'
import type { SessionEvent } from '../events'

function run(events: SessionEvent[]) {
  return events.reduce(transcriptReducer, initialTranscript)
}

describe('turnModel reducer', () => {
  it('stamps model onto the addressed main-thread turn', () => {
    const s = run([
      { type: 'turnStart', turnId: 't1' },
      { type: 'turnModel', turnId: 't1', model: 'claude-opus-4-8' },
    ])
    expect(s.turns.find((t) => t.id === 't1')?.model).toBe('claude-opus-4-8')
  })

  it('stamps model onto the subthread when parentToolUseId is set', () => {
    const s = run([
      { type: 'turnStart', turnId: 'st1', parentToolUseId: 'task1' },
      { type: 'turnModel', turnId: 'st1', model: 'claude-haiku-4-5', parentToolUseId: 'task1' },
    ])
    expect(s.subAgents['task1']?.model).toBe('claude-haiku-4-5')
  })

  it('ignores turnModel for an unknown main-thread turn (no crash, no turn created)', () => {
    const s = run([{ type: 'turnModel', turnId: 'ghost', model: 'claude-opus-4-8' }])
    expect(s.turns).toHaveLength(0)
  })
})
