// @vitest-environment node
import { describe, expect, it } from 'vitest'
import type { Turn } from '../reducer'
import { failedPrompts, recentRewindTargets, retryablePrompt, rewindBlocked, rewindTargetFor } from '../rewindTarget'

const UUID = '0b9c7a52-6f1e-4c55-9d1e-2f3a4b5c6d7e'
const user = (id: string, extra: Partial<Turn> = {}): Turn =>
  ({ id, role: 'user', blocks: [{ kind: 'text', id: `${id}:u`, text: `ask ${id}` }], ...extra })

describe('rewindTargetFor', () => {
  it('targets a live prompt once the CLI echoed its uuid, by its own id', () => {
    expect(rewindTargetFor(user('9f8e7d6c5b4a39281706f5e4d3c2b1a0', { promptUuid: UUID }))).toBe('9f8e7d6c5b4a39281706f5e4d3c2b1a0')
  })

  it('targets a replayed prompt, whose id is the uuid', () => {
    expect(rewindTargetFor(user(UUID))).toBe(UUID)
  })

  it('offers nothing for a live prompt with no echo yet, a pending send, an owner prefix or a reply', () => {
    expect(rewindTargetFor(user('9f8e7d6c5b4a39281706f5e4d3c2b1a0'))).toBeNull()
    expect(rewindTargetFor(user(UUID, { pending: 'sent' }))).toBeNull()
    expect(rewindTargetFor(user(UUID, { ownerPrefix: true }))).toBeNull()
    expect(rewindTargetFor({ id: UUID, role: 'assistant', blocks: [] })).toBeNull()
  })
})

describe('recentRewindTargets', () => {
  it('lists the targetable prompts newest first, at most 20', () => {
    const turns: Turn[] = Array.from({ length: 25 }, (_, i) => user(`h${i}`, { promptUuid: `u${i}` }))
    turns.splice(3, 0, user('no-target'), { id: 'a', role: 'assistant', blocks: [] })
    const got = recentRewindTargets(turns).map((t) => t.id)
    expect(got).toHaveLength(20)
    expect(got[0]).toBe('h24')
    expect(got.at(-1)).toBe('h5')
    expect(got).not.toContain('no-target')
  })
})

describe('rewindBlocked — exactly the states the server refuses (driver.rewind_busy)', () => {
  it.each(['in_progress', 'working', 'retrying', 'condensing', 'waiting_input', 'waiting_question', 'ready_review'])(
    'blocks %s', (state) => expect(rewindBlocked(state)).toBe(true))
  it.each(['idle', 'done', 'handed_back', 'needs_attention', 'limited', 'complete', 'pending_approval'])(
    'allows %s', (state) => expect(rewindBlocked(state)).toBe(false))
})

describe("failedPrompts — the latest prompt's own failed replies (Rewind & retry)", () => {
  const reply = (id: string, failed: boolean, extra: Partial<Turn> = {}): Turn => ({ id, role: 'assistant', blocks: failed
    ? [{ kind: 'error', id: `${id}:err:0`, code: 'api_error', message: 'boom' }]
    : [{ kind: 'text', id: `${id}:t`, text: 'ok' }], ...extra })

  it("maps the latest prompt's failed reply to that prompt", () => {
    const p1 = user('p1'), p2 = user('p2')
    const got = failedPrompts([p1, reply('a1', false), p2, reply('a2', true)])
    expect([...got.keys()]).toEqual(['a2'])
    expect(got.get('a2')).toBe(p2)
  })

  it("never maps a second reply with another engine turn — not the prompt's own (a2b)", () => {
    const p2 = user('p2')
    const got = failedPrompts([p2, reply('a2', true), reply('a2b', true)])
    expect([...got.keys()]).toEqual(['a2'])
  })

  it('maps a steered #after: segment, which shares its engine turn, to the steering prompt', () => {
    const p1 = user('p1'), p2 = user('p2')
    const seg = reply('t1#after:p2', true, { engineTurnId: 't1' })
    const got = failedPrompts([p1, reply('t1', false), p2, seg])
    expect(got.get('t1#after:p2')).toBe(p2)
  })

  it("maps a later segment of the prompt's own engine turn", () => {
    const p1 = user('p1')
    const got = failedPrompts([p1, reply('t1', false), reply('t1#x', true, { engineTurnId: 't1' })])
    expect(got.get('t1#x')).toBe(p1)
  })

  it('leaves out an autonomous turn (a background notification) after the reply — it has no prompt', () => {
    const got = failedPrompts([user('p1'), reply('t1', false), reply('t9', true)])
    expect(got.size).toBe(0)
  })

  it('leaves out a fabricated turn, and a fabricated tail does not break the mapping', () => {
    const p1 = user('p1')
    expect(failedPrompts([p1, reply('t1', false), reply('card:x', true, { fabricated: true })]).size).toBe(0)
    const got = failedPrompts([p1, reply('card:x', false, { fabricated: true }), reply('t1', true)])
    expect(got.get('t1')).toBe(p1)
  })

  it('maps only the LAST prompt: an older failure is left out', () => {
    const got = failedPrompts([user('p1'), reply('a1', true), user('p2'), reply('a2', false)])
    expect(got.size).toBe(0)
  })

  it('leaves out a failed reply with no prompt before it', () => {
    expect(failedPrompts([reply('a0', true)]).size).toBe(0)
  })
})

describe('retryablePrompt — a prompt Rewind & retry can resend as typed', () => {
  it('accepts a targeted, typed, text-only prompt', () => {
    expect(retryablePrompt(user(UUID))).toBe(true)
  })
  it('refuses a prompt with images, with no text, from a nudge, or with no target', () => {
    expect(retryablePrompt({ ...user(UUID), blocks: [
      { kind: 'text', id: 't', text: 'look' },
      { kind: 'image', id: 'i', images: [{ url: 'u', mime: 'image/png', name: 'a.png' }] }] })).toBe(false)
    expect(retryablePrompt({ ...user(UUID), blocks: [{ kind: 'text', id: 't', text: '  ' }] })).toBe(false)
    expect(retryablePrompt(user(UUID, { origin: { kind: 'autopilot' } }))).toBe(false)
    expect(retryablePrompt(user('9f8e7d6c5b4a39281706f5e4d3c2b1a0'))).toBe(false)
  })
})
