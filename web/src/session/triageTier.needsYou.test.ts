import { describe, it, expect } from 'vitest'
import type { Session } from '@/api/types'
import { needsYouCount } from './triageTier'

function session(over: Partial<Session> = {}): Session {
  return {
    id: 'a', name: 'a', accountName: 'acct', engine: 'claude_code',
    engineSessionId: null, status: 'idle',
    createdAt: '2024-01-01T00:00:00Z', prs: [],
    ...over,
  }
}

// The queue number. It is the `needs-you` tier of the ONE triage read
// (`sessionTriage`), not a second status list — so whatever the card pill
// calls "Needs you", this counts, and nothing else.
describe('needsYouCount', () => {
  it('counts every status the triage read puts in the needs-you tier', () => {
    const n = needsYouCount([
      session({ id: '1', status: 'waiting_input' }),
      session({ id: '2', status: 'waiting_question' }),
      session({ id: '3', status: 'handed_back' }),
      session({ id: '4', status: 'ready_review' }),
      session({ id: '5', status: 'pending_approval' }),
    ])
    expect(n).toBe(5)
  })

  it('ignores calm, busy, waiting and finished sessions', () => {
    const n = needsYouCount([
      session({ id: '1', status: 'idle' }),
      session({ id: '2', status: 'done' }),
      session({ id: '3', status: 'in_progress' }),
      session({ id: '4', status: 'waiting_external' }),
      session({ id: '5', status: 'queued' }),
      session({ id: '6', status: 'complete' }),
    ])
    expect(n).toBe(0)
  })

  it('follows the restart-strand relabel, exactly as the pill does', () => {
    // An idle session whose turn a daemon restart dropped reads "Interrupted —
    // re-send to resume", a needs-you pill; the count agrees with the pill.
    expect(needsYouCount([session({ status: 'idle', interruptedByRestart: true })])).toBe(1)
  })

  it('is 0 for an empty list', () => {
    expect(needsYouCount([])).toBe(0)
  })
})
