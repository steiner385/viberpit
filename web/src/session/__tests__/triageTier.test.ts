// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { triageTier, sessionTriage } from '../triageTier'
import { statusPresentation } from '../statusTier'
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
    // but there's no action to take, so it must not pull the card into "Failed".
    expect(triageTier(s('done', 'failing', 'closed'))).toBe('your-turn')
  })
  it('does NOT elevate a done session for a MERGED PR with frozen failing checks', () => {
    expect(triageTier(s('done', 'failing', 'merged'))).toBe('your-turn')
  })
  it('does NOT elevate a waiting_external session for a failing PR (an agent is monitoring it)', () => {
    // A session with an armed Monitor/wakeup (waiting_external, "Waiting…") is
    // already watching its CI — a red rollup is that agent's job, not the user's.
    // It must stay in the Waiting group, not jump to Failed.
    expect(triageTier(s('waiting_external', 'failing'))).toBe('waiting')
  })
  it('does NOT elevate a live (in_progress) session for a failing PR', () => {
    // A turn is actively running — likely fixing the very failure. Not user-facing.
    expect(triageTier(s('in_progress', 'failing'))).toBe('busy')
  })
})

describe('sessionTriage (pill + group share one computation)', () => {
  it('an elevated at-rest session reads "Checks failing", not its calm status label', () => {
    // The whole point of Fix B: when a failing PR lifts a done session into the
    // error tier, the card pill must SAY so — not show "Your turn" under a red
    // "Failed" header. Pill and group can no longer disagree.
    const p = sessionTriage(s('done', 'failing'))
    expect(p.tier).toBe('error')
    expect(p.label).toBe('Checks failing')
  })

  it('a non-elevated session returns its plain status presentation unchanged', () => {
    // waiting_external is NOT elevated (an agent monitors it) — the pill stays "Waiting…".
    const waiting = sessionTriage(s('waiting_external', 'failing'))
    expect(waiting).toEqual(statusPresentation('waiting_external'))
    const done = sessionTriage(s('done', 'passing'))
    expect(done).toEqual(statusPresentation('done'))
  })

  it('triageTier is exactly sessionTriage(...).tier — one source, no parallel path', () => {
    for (const st of ['idle', 'done', 'in_progress', 'waiting_external', 'waiting_input']) {
      for (const checks of [undefined, 'passing', 'failing'] as const) {
        const sess = s(st, checks)
        expect(triageTier(sess)).toBe(sessionTriage(sess).tier)
      }
    }
  })

  it('an interrupted-by-restart session is NOT overridden by a failing PR (pill == group)', () => {
    // Latent second disagreement: interrupted idle shows an amber "re-send" pill
    // but used to group under red "Failed". Now both read needs-you.
    const sess = { ...s('idle', 'failing'), interruptedByRestart: true } as Session
    const p = sessionTriage(sess)
    expect(p.tier).toBe('needs-you')
    expect(p.label).toBe('Interrupted — re-send to resume')
    expect(triageTier(sess)).toBe('needs-you')
  })
})
