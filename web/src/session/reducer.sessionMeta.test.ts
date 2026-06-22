import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

describe('sessionMeta carries account + pin', () => {
  it('updates model, account, and accountPinned', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'sessionMeta', model: 'opus', sessionId: 's1',
      account: 'acct-b', accountPinned: true,
    } as never)
    expect(s.model).toBe('opus')
    expect(s.account).toBe('acct-b')
    expect(s.accountPinned).toBe(true)
  })
})
