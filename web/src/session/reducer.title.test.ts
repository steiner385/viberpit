import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

describe('transcriptReducer — title', () => {
  it('updates the header title from a Claude-sourced title event', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'title', title: 'Investigate slow query', source: 'claude',
    })
    expect(s.title).toBe('Investigate slow query')
  })

  it('a later Claude title supersedes an earlier prompt-sourced one', () => {
    const s1 = transcriptReducer(initialTranscript, {
      type: 'title', title: 'fix the', source: 'prompt',
    })
    const s2 = transcriptReducer(s1, {
      type: 'title', title: 'Fix flaky auth test', source: 'claude',
    })
    expect(s2.title).toBe('Fix flaky auth test')
  })
})
