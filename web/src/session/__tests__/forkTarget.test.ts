import { describe, it, expect } from 'vitest'
import { forkTargetFor, latestReplyTarget } from '@/session/forkTarget'
import type { Turn } from '@/session/reducer'

const user = (id: string, text: string, extra: Partial<Turn> = {}): Turn =>
  ({ id, role: 'user', blocks: [{ kind: 'text', id: `${id}:u`, text }], ...extra })
const reply = (id: string, extra: Partial<Turn> = {}): Turn =>
  ({ id, role: 'assistant', blocks: [{ kind: 'text', id: `${id}:t`, text: 'ok' }], ...extra })

describe('forkTargetFor', () => {
  it('forks a reply through its own anchor, keyed by the engine turn id', () => {
    const r = reply('t1#after:x', { engineTurnId: 't1', anchorUuid: 'u1' })
    expect(forkTargetFor([r], r, 0)).toEqual({ atTurnId: 't1', mode: 'reply' })
    const plain = reply('t2', { anchorUuid: 'u2' })
    expect(forkTargetFor([plain], plain, 0)).toEqual({ atTurnId: 't2', mode: 'reply' })
  })

  it('offers nothing for a reply without an anchor', () => {
    const r = reply('t1')
    expect(forkTargetFor([r], r, 0)).toBeNull()
  })

  it('forks a prompt at the reply before it, with the prompt as the prefill', () => {
    const turns = [user('u1', 'first'), reply('t1', { anchorUuid: 'a1' }), user('u2', 'second'), reply('t2')]
    expect(forkTargetFor(turns, turns[2], 0)).toEqual({ atTurnId: 't1', mode: 'message', prefill: 'second' })
  })

  it('forks the first message with no cut, only when it really is the first', () => {
    const turns = [user('u1', 'first'), reply('t1', { anchorUuid: 'a1' })]
    expect(forkTargetFor(turns, turns[0], 0)).toEqual({ atTurnId: null, mode: 'message', prefill: 'first' })
    // An older page is not loaded: the reply before it exists but is not here.
    expect(forkTargetFor(turns, turns[0], 3)).toBeNull()
  })

  it('offers nothing when the reply before the prompt is unanchored or another prompt', () => {
    const interrupted = [user('u1', 'a'), reply('t1'), user('u2', 'b')]
    expect(forkTargetFor(interrupted, interrupted[2], 0)).toBeNull()
    const queued = [user('u1', 'a'), reply('t1', { anchorUuid: 'a1' }), user('u2', 'b'), user('u3', 'c')]
    expect(forkTargetFor(queued, queued[3], 0)).toBeNull()
  })

  it('offers nothing for an unconfirmed prompt, one without text, or a turn not in the transcript', () => {
    const pending = user('pending:1', 'x', { pending: 'buffered' })
    expect(forkTargetFor([pending], pending, 0)).toBeNull()
    const image: Turn = { id: 'u1', role: 'user', blocks: [{ kind: 'image', id: 'i', images: [] }] }
    expect(forkTargetFor([image], image, 0)).toBeNull()
    expect(forkTargetFor([], user('elsewhere', 'x'), 0)).toBeNull()
  })
})

describe('latestReplyTarget', () => {
  it('is the most recent anchored reply', () => {
    const turns = [reply('t1', { anchorUuid: 'a1' }), user('u2', 'b'), reply('t2', { anchorUuid: 'a2' }), user('u3', 'c'), reply('t3')]
    expect(latestReplyTarget(turns)).toEqual({ atTurnId: 't2', mode: 'reply' })
  })

  it('is null with no anchored reply', () => {
    expect(latestReplyTarget([user('u1', 'a'), reply('t1')])).toBeNull()
  })
})
