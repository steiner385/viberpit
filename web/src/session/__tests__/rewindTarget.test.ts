// @vitest-environment node
import { describe, expect, it } from 'vitest'
import type { Turn } from '../reducer'
import { recentRewindTargets, rewindTargetFor } from '../rewindTarget'

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
