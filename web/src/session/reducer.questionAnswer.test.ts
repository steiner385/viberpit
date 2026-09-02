import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { SessionEvent } from './events'

// A composer prompt sent while a question is pending is delivered by the server
// as the question's answer (chat mode) — it never echoes as a `user` turn, so
// nothing else would ever consume the tracked pendingSent entry and the next
// replayComplete would resurrect the answer as a phantom "pending" bubble.
const DONE: SessionEvent = { type: 'replayComplete' }
const question: SessionEvent = {
  type: 'question', questionId: 'q1', turnId: 't1', header: 'Pick', question: 'Which?',
  options: [{ label: 'A' }, { label: 'B' }], multiSelect: false,
}

describe('transcriptReducer — questionResolved consumes the matching sent prompt', () => {
  it('drops the pendingSent entry whose text became the chat answer', () => {
    let s = transcriptReducer(initialTranscript, DONE)
    s = transcriptReducer(s, question)
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'all of the above' })
    expect(s.pendingSent).toHaveLength(1)
    s = transcriptReducer(s, {
      type: 'questionResolved', questionId: 'q1', turnId: 't1',
      selected: ['all of the above'], mode: 'chat',
    })
    expect(s.pendingSent).toEqual([])
    const q = s.turns.flatMap((t) => t.blocks).find((b) => b.kind === 'question')
    expect(q && q.kind === 'question' ? q.answered : undefined).toEqual(['all of the above'])
    expect(q && q.kind === 'question' ? q.live : undefined).toBe(false)
  })

  it('leaves unrelated pendingSent entries alone', () => {
    let s = transcriptReducer(initialTranscript, DONE)
    s = transcriptReducer(s, question)
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'something else' })
    s = transcriptReducer(s, {
      type: 'questionResolved', questionId: 'q1', turnId: 't1', selected: ['A'], mode: 'option',
    })
    expect(s.pendingSent).toEqual([{ id: 'pending:0', text: 'something else' }])
  })
})
