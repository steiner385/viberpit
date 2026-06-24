import { describe, it, expect } from 'vitest'
import { triageTier } from '../triageTier'
import type { Session, PullRequestInfo } from '@/api/types'

const s = (
  status: string,
  checks?: 'passing'|'failing'|'pending'|'none',
  state: PullRequestInfo['state'] = 'open',
): Session =>
  ({ id: 'x', name: 'n', status, prs: checks ? [{ number: 1, url: 'u', state, checks, title: 't', deployed: false } as PullRequestInfo] : [] } as Session)

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
  it('does NOT elevate a done session for a CLOSED PR with frozen failing checks', () => {
    // A closed PR is terminal: GitHub freezes its statusCheckRollup at red,
    // but there's no action to take, so it must not pull the card into "Needs attention".
    expect(triageTier(s('done', 'failing', 'closed'))).toBe('your-turn')
  })
  it('does NOT elevate a done session for a MERGED PR with frozen failing checks', () => {
    expect(triageTier(s('done', 'failing', 'merged'))).toBe('your-turn')
  })
})
