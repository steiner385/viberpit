// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { SessionEvent } from './events'

function notice(over: Partial<SessionEvent & { type: 'notice' }> = {}): SessionEvent {
  return {
    type: 'notice', kind: 'automation', title: 'Backlog runner errored', body: 'acme/checkout failed',
    ts: 1_000_000, ...over,
  } as SessionEvent
}

describe('notice reducer', () => {
  it('appends a noticeCard block in its own turn', () => {
    const s = transcriptReducer(initialTranscript, notice())
    const last = s.turns.at(-1)!
    expect(last.blocks).toHaveLength(1)
    expect(last.blocks[0]).toMatchObject({
      kind: 'noticeCard', noticeKind: 'automation', title: 'Backlog runner errored', body: 'acme/checkout failed', ts: 1_000_000,
    })
  })

  it('does not disturb the rest of the transcript', () => {
    const seeded = transcriptReducer(initialTranscript, { type: 'user', turnId: 't1', text: 'hi' } as SessionEvent)
    const s = transcriptReducer(seeded, notice())
    expect(s.turns).toHaveLength(2)
    expect(s.turns[0].id).toBe('t1')
  })

  it('two notices within 24h stay as separate cards', () => {
    const s1 = transcriptReducer(initialTranscript, notice({ ts: 1_000_000, title: 'first' }))
    const s2 = transcriptReducer(s1, notice({ ts: 1_000_000 + 60 * 60 * 1000, title: 'second' }))
    const cards = s2.turns.flatMap((t) => t.blocks).filter((b) => b.kind === 'noticeCard')
    expect(cards).toHaveLength(2)
  })

  it('collapses a notice older than 24h relative to a new arrival into a count', () => {
    const DAY_MS = 24 * 60 * 60 * 1000
    const s1 = transcriptReducer(initialTranscript, notice({ ts: 1_000_000, title: 'stale' }))
    const s2 = transcriptReducer(s1, notice({ ts: 1_000_000 + DAY_MS + 1, title: 'fresh' }))
    const blocks = s2.turns.flatMap((t) => t.blocks)
    const cards = blocks.filter((b) => b.kind === 'noticeCard')
    const collapsed = blocks.filter((b) => b.kind === 'noticeCollapsed')
    expect(cards).toHaveLength(1)
    expect(cards[0]).toMatchObject({ title: 'fresh' })
    expect(collapsed).toHaveLength(1)
    expect(collapsed[0]).toMatchObject({ count: 1 })
  })

  it('a conversation turn in between stops the collapse scan', () => {
    const DAY_MS = 24 * 60 * 60 * 1000
    const s1 = transcriptReducer(initialTranscript, notice({ ts: 1_000_000, title: 'old' }))
    const s2 = transcriptReducer(s1, { type: 'user', turnId: 't1', text: 'hi' } as SessionEvent)
    const s3 = transcriptReducer(s2, notice({ ts: 1_000_000 + DAY_MS + 1, title: 'new' }))
    const cards = s3.turns.flatMap((t) => t.blocks).filter((b) => b.kind === 'noticeCard')
    const collapsed = s3.turns.flatMap((t) => t.blocks).filter((b) => b.kind === 'noticeCollapsed')
    // The old notice is separated from the new one by a conversation turn, so it is
    // never scanned for collapse — both notices remain as individual cards.
    expect(cards).toHaveLength(2)
    expect(collapsed).toHaveLength(0)
  })
})
