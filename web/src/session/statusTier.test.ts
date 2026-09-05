// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { statusPresentation, statusTier, sortSessionsByTier, TIER_ACCENT } from './statusTier'
import type { StatusTier } from './statusTier'

describe('retrying presentation', () => {
  it('is amber, busy tier, overloaded label', () => {
    const p = statusPresentation('retrying')
    expect(p.label).toBe('Overloaded — retrying')
    expect(p.tier).toBe('busy')
    expect(p.textClass).toContain('warning')
  })
  it('unknown status falls back to a neutral Unknown, never Ready', () => {
    expect(statusTier('nonsense')).toBe('waiting')
    expect(statusPresentation('nonsense').label).toBe('Unknown')
  })
})

describe('interrupted-by-restart presentation (issue #434)', () => {
  it('relabels an idle session as an actionable re-send prompt', () => {
    const p = statusPresentation('idle', { interruptedByRestart: true })
    expect(p.tier).toBe('needs-you')
    expect(p.label).toMatch(/re-send/i)
  })
  it('also relabels a needs_attention session interrupted by a restart', () => {
    const p = statusPresentation('needs_attention', { interruptedByRestart: true })
    expect(p.tier).toBe('needs-you')
    expect(p.label).toMatch(/re-send/i)
  })
  it('leaves a plain idle session as the calm Ready pill', () => {
    expect(statusPresentation('idle').label).toBe('Ready')
    expect(statusPresentation('idle', { interruptedByRestart: false }).label).toBe('Ready')
  })
  it('does not hijack an in_progress session even if the flag lingers', () => {
    expect(statusPresentation('in_progress', { interruptedByRestart: true }).label).toBe('Working…')
  })
})

describe('condensing status', () => {
  it('presents as a busy-tier Condensing pill', () => {
    const p = statusPresentation('condensing')
    expect(p.tier).toBe('busy')
    expect(p.label).toBe('Condensing…')
  })
  it('is grouped with the busy tier', () => {
    expect(statusTier('condensing')).toBe('busy')
  })
})

describe('TIER_ACCENT rail/dot opacity hierarchy', () => {
  // The muted rail is deliberate: a rail at full strength would compete with the
  // status dot. Family-only assertions (/neutral/) pass either way, so pin the opacity.
  it('mutes the your-turn rail relative to its dot', () => {
    expect(TIER_ACCENT['your-turn'].rail).toContain('/40')
    expect(TIER_ACCENT['your-turn'].dot).not.toContain('/40')
  })
  it('mutes the waiting rail relative to its dot', () => {
    expect(TIER_ACCENT.waiting.rail).toContain('/40')
    expect(TIER_ACCENT.waiting.dot).not.toContain('/40')
  })
})

describe('TIER_ACCENT pulse gating', () => {
  const PULSING: StatusTier[] = ['needs-you', 'error']
  it.each(Object.keys(TIER_ACCENT) as StatusTier[])('pulses only on action-required tiers: %s', (tier) => {
    expect(TIER_ACCENT[tier].pulse).toBe(PULSING.includes(tier))
  })
})

describe('liveness demotion (spec 002 FR-104)', () => {
  it.each(['in_progress', 'waiting_input', 'idle', 'done', 'queued'])(
    'demotes non-terminal %s when liveness is not fresh', (status) => {
      expect(statusPresentation(status, { liveness: 'unreachable' }).tier).toBe('unreachable')
    })
  it.each(['complete', 'ended'])('never demotes terminal %s', (status) => {
    expect(statusPresentation(status, { liveness: 'unreachable' }).tier).not.toBe('unreachable')
  })
  it('fresh (and absent) liveness changes nothing', () => {
    expect(statusPresentation('in_progress', { liveness: 'fresh' }).tier).toBe('busy')
    expect(statusPresentation('in_progress').tier).toBe('busy')
  })
  it('demotes for stale/dead too, not only unreachable', () => {
    expect(statusPresentation('in_progress', { liveness: 'stale' }).tier).toBe('unreachable')
    expect(statusPresentation('in_progress', { liveness: 'dead' }).tier).toBe('unreachable')
  })

  // #961 item 3: liveness demotion outranks the interrupted-by-restart relabel — a
  // dead daemon cannot honor a "re-send to resume" affordance any more than a "Ready" one.
  it('a non-fresh host outranks interruptedByRestart (no false re-send affordance)', () => {
    const p = statusPresentation('idle', { interruptedByRestart: true, liveness: 'unreachable' })
    expect(p.tier).toBe('unreachable')
    expect(p.label).not.toMatch(/re-send/i)
  })
  it('still shows the re-send prompt when the host is fresh', () => {
    const p = statusPresentation('idle', { interruptedByRestart: true, liveness: 'fresh' })
    expect(p.label).toMatch(/re-send/i)
  })

  // #961 item 4: a failed snapshot ('unknown') is visibly degraded but distinct from a
  // confirmed-unreachable host — its own gentler pill, never a silent "fresh" (NFR-5).
  it.each(['in_progress', 'idle', 'done'])('renders unknown liveness as its own pill for %s', (status) => {
    const p = statusPresentation(status, { liveness: 'unknown' })
    expect(p.tier).toBe('unreachable')       // reuses the muted tier for color/sort
    expect(p.label).toBe('Status unknown')   // …but is labelled distinctly, not "Unreachable"
  })
  it.each(['complete', 'ended'])('never demotes terminal %s even when liveness is unknown', (status) => {
    expect(statusPresentation(status, { liveness: 'unknown' }).label).not.toBe('Status unknown')
  })
})

describe('unknown status (spec 002 FR-402)', () => {
  it('renders neutral Unknown, never Ready', () => {
    const p = statusPresentation('some_future_status')
    expect(p.label).toBe('Unknown')
    expect(p.tier).toBe('waiting')
  })
})

// Merged from the former web/src/session/__tests__/statusTier.test.ts (#961 item 6):
// these exhaustive status→tier and status→label tables plus the sort coverage lived
// ONLY in that duplicate copy, while the liveness/accent suites above lived only here.
// Consolidated into this single colocated file so the two can no longer drift apart.
describe('statusTier mapping table', () => {
  it.each([
    ['in_progress', 'busy'],
    ['waiting_input', 'needs-you'],
    ['waiting_question', 'needs-you'],
    ['ready_review', 'needs-you'],
    ['done', 'your-turn'],
    ['complete', 'your-turn'],
    ['idle', 'your-turn'],
    ['needs_attention', 'error'],
    ['limited', 'error'],
    ['ended', 'your-turn'],
    ['waiting_external', 'waiting'],   // parked on an external event
    ['queued', 'waiting'],          // enqueued behind the active-session cap
    ['pending_approval', 'needs-you'],  // parked on a human model decision
    ['working', 'busy'],            // legacy alias tolerated
  ])('maps %s -> %s', (state, tier) => {
    expect(statusTier(state)).toBe(tier)
  })

  it('falls back to neutral "Unknown" (waiting tier) for an unknown state', () => {
    expect(statusTier('totally-unknown')).toBe('waiting')
    expect(statusPresentation('totally-unknown').label).toBe('Unknown')
  })
})

describe('statusPresentation label table', () => {
  it.each([
    ['in_progress', 'Working…'],
    ['ready_review', 'Approve plan'],
    ['waiting_input', 'Permission needed'],
    ['waiting_question', 'Answer needed'],
    ['done', 'Your turn'],
    ['complete', 'Complete'],
    ['idle', 'Ready'],
    ['needs_attention', 'Turn failed'],
    ['limited', 'Usage limit'],
    ['ended', 'Ended'],
    ['waiting_external', 'Waiting…'],
    ['queued', 'Queued'],
    ['pending_approval', 'Awaiting approval'],
  ])('labels %s as "%s"', (state, label) => {
    expect(statusPresentation(state).label).toBe(label)
  })

  // Regression: a session that has not started yet used to hit the FALLBACK and
  // render as a calm "Ready" — indistinguishable from a live idle session, over an
  // empty conversation. That is what made a restart-stranded session read as a
  // healthy-but-dead orphan. 'queued'/'pending_approval' were in the SessionStatus
  // union but missing from the presentation table; PRESENTATION is now typed
  // Record<SessionStatus | 'working', …> so tsc fails the build if that drifts again.
  it.each(['queued', 'pending_approval'])(
    'never renders %s as the "Ready" fallback', (state) => {
      const p = statusPresentation(state)
      expect(p.label).not.toBe('Ready')
      expect(p.tier).not.toBe('your-turn')
    })
})

describe('sortSessionsByTier', () => {
  it('sorts needs-you first, then error, your-turn, busy; stable within tier', () => {
    const input = [
      { id: 'a', name: 'A', status: 'in_progress' },
      { id: 'b', name: 'B', status: 'done' },
      { id: 'c', name: 'C', status: 'waiting_input' },
      { id: 'd', name: 'D', status: 'in_progress' },
      { id: 'e', name: 'E', status: 'needs_attention' },
    ]
    const result = sortSessionsByTier(input)
    expect(result.map((s) => s.id)).toEqual(['c', 'e', 'b', 'a', 'd'])
  })

  it('ranks waiting (parked) below your-turn and above busy', () => {
    const input = [
      { id: 'busy', status: 'in_progress' },
      { id: 'wait', status: 'waiting_external' },
      { id: 'turn', status: 'done' },
    ]
    const result = sortSessionsByTier(input)
    expect(result.map((s) => s.id)).toEqual(['turn', 'wait', 'busy'])
  })

  it('does not mutate the original array', () => {
    const input = [
      { id: 'x', status: 'in_progress' },
      { id: 'y', status: 'waiting_input' },
    ]
    const original = [...input]
    sortSessionsByTier(input)
    expect(input).toEqual(original)
  })
})

describe('usage-stalled presentation', () => {
  it('relabels a usage-paused needs_attention session as waiting for capacity', () => {
    const p = statusPresentation('needs_attention', { usageStalled: true, pausedUntil: '2026-09-04T16:10:00+00:00' })
    expect(p.tier).toBe('waiting')
    expect(p.label).toMatch(/^Waiting for capacity · retries /)
  })
  it('omits the time suffix when no reset is known', () => {
    expect(statusPresentation('needs_attention', { usageStalled: true, pausedUntil: null }).label).toBe('Waiting for capacity')
  })
  it('labels a pinned limited session and names the reset', () => {
    const p = statusPresentation('limited', { usageStalled: true, pausedUntil: '2026-09-04T16:10:00+00:00' })
    expect(p.tier).toBe('waiting')
    expect(p.label).toMatch(/^Usage limit \(pinned\) · resets /)
  })
  it('leaves a plain needs_attention (crash cap) as Turn failed', () => {
    expect(statusPresentation('needs_attention', { usageStalled: false }).label).toBe('Turn failed')
    expect(statusPresentation('needs_attention').tier).toBe('error')
  })
  it('never hijacks a live status even if the flag lingers', () => {
    expect(statusPresentation('in_progress', { usageStalled: true }).label).toBe('Working…')
  })
  it('restart interruption wins over the stall relabel', () => {
    expect(statusPresentation('needs_attention', { usageStalled: true, interruptedByRestart: true }).label).toMatch(/re-send/i)
  })
})
