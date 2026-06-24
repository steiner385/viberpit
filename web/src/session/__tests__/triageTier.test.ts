import { describe, it, expect } from 'vitest'
import { triageTier } from '../triageTier'
import type { Session } from '@/api/types'

const s = (status: string, checks?: 'passing'|'failing'|'pending'|'none'): Session =>
  ({ id: 'x', name: 'n', status, pr: checks ? ({ number: 1, url: 'u', state: 'open', checks, title: 't' } as any) : null } as Session)

describe('triageTier', () => {
  it('elevates a done session with a failing PR to error tier (AUT-03)', () => {
    expect(triageTier(s('done', 'failing'))).toBe('error')
  })
  it('leaves a done session with a green PR at your-turn', () => {
    expect(triageTier(s('done', 'passing'))).toBe('your-turn')
  })
  it('keeps needs-you above a failing PR (more urgent status wins)', () => {
    expect(triageTier(s('waiting_input', 'failing'))).toBe('needs-you')
  })
  it('no PR → falls back to status tier', () => {
    expect(triageTier(s('idle'))).toBe('your-turn')
  })
})
