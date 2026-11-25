// @vitest-environment node
/**
 * Asks answered in the Claude app over Remote Control (`via: 'claude_app'`): the card
 * resolves, says where it was answered, and picks up the outcome once the server reads
 * it off the tool result — including across a mid-turn reattach's ring resupply.
 */
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { TranscriptState } from './reducer'
import type { SessionEvent } from './events'

function reduce(events: SessionEvent[], initial = initialTranscript): TranscriptState {
  return events.reduce((s, ev) => transcriptReducer(s, ev), initial)
}

const blocks = (s: TranscriptState) => s.turns.flatMap((t) => t.blocks)
const notices = (s: TranscriptState) =>
  blocks(s).flatMap((b) => (b.kind === 'notice' ? [b.text] : []))

const bashCard: SessionEvent[] = [
  { type: 'turnStart', turnId: 't1' },
  { type: 'permissionRequest', permId: 'p1', turnId: 't1', tool: 'Bash', toolKind: 'bash', input: { command: 'npm test' }, plan: false },
]

describe('permission answered in the Claude app', () => {
  it('clears the card and says where it was answered', () => {
    const s = reduce([...bashCard, { type: 'permissionResolved', permId: 'p1', turnId: 't1', allowed: null, via: 'claude_app' }])
    expect(blocks(s).some((b) => b.kind === 'permission')).toBe(false)
    expect(notices(s)).toEqual(['Answered in the Claude app: Bash: npm test'])
  })

  it.each([
    [true, 'Allowed Bash: npm test — answered in the Claude app'],
    [false, 'Denied Bash: npm test — answered in the Claude app'],
  ])('rewrites the same line once the outcome (%s) is known', (allowed, text) => {
    const s = reduce([
      ...bashCard,
      { type: 'permissionResolved', permId: 'p1', turnId: 't1', allowed: null, via: 'claude_app' },
      { type: 'permissionResolved', permId: 'p1', turnId: 't1', allowed, via: 'claude_app' },
    ])
    expect(notices(s)).toEqual([text])
  })

  it('names a plan approval in plan terms', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'permissionRequest', permId: 'p2', turnId: 't1', tool: 'ExitPlanMode', toolKind: 'unknown', input: { plan: 'x' }, plan: true },
      { type: 'permissionResolved', permId: 'p2', turnId: 't1', allowed: null, via: 'claude_app' },
    ])
    expect(notices(s)).toEqual(['Answered in the Claude app: plan approval'])
    const t = transcriptReducer(s, { type: 'permissionResolved', permId: 'p2', turnId: 't1', allowed: true, via: 'claude_app' })
    expect(notices(t)).toEqual(['Approved plan — running — answered in the Claude app'])
  })

  it('a reattach resupply replaying every frame leaves one resolved line, no live card', () => {
    const frames: SessionEvent[] = [
      ...bashCard,
      { type: 'permissionResolved', permId: 'p1', turnId: 't1', allowed: null, via: 'claude_app' },
      { type: 'permissionResolved', permId: 'p1', turnId: 't1', allowed: true, via: 'claude_app' },
    ]
    const once = reduce(frames)
    const twice = reduce(frames.slice(1), once)
    expect(blocks(twice).some((b) => b.kind === 'permission')).toBe(false)
    expect(notices(twice)).toEqual(['Allowed Bash: npm test — answered in the Claude app'])
  })
})

describe('question answered in the Claude app', () => {
  const ask: SessionEvent[] = [
    { type: 'turnStart', turnId: 't1' },
    { type: 'question', questionId: 'q1', turnId: 't1', header: 'DB', question: 'Which database?', options: [{ label: 'Postgres' }, { label: 'SQLite' }], multiSelect: false },
  ]
  const question = (s: TranscriptState) => blocks(s).find((b) => b.kind === 'question')

  it('resolves in place, marked as answered in the Claude app, then shows the answer', () => {
    const first = reduce([...ask, { type: 'questionResolved', questionId: 'q1', turnId: 't1', selected: null, via: 'claude_app' }])
    expect(question(first)).toMatchObject({ live: false, answered: [], answeredVia: 'claude_app' })
    const second = transcriptReducer(first, { type: 'questionResolved', questionId: 'q1', turnId: 't1', selected: ['Postgres'], via: 'claude_app' })
    expect(question(second)).toMatchObject({ live: false, answered: ['Postgres'], answeredVia: 'claude_app' })
  })

  it('a replayed label-less frame never erases a known answer, nor reopens the card', () => {
    const frames: SessionEvent[] = [
      ...ask,
      { type: 'questionResolved', questionId: 'q1', turnId: 't1', selected: null, via: 'claude_app' },
      { type: 'questionResolved', questionId: 'q1', turnId: 't1', selected: ['SQLite'], via: 'claude_app' },
    ]
    // The ring resupply re-delivers the question and both resolutions, in order.
    const s = reduce(frames.slice(1), reduce(frames))
    expect(question(s)).toMatchObject({ live: false, answered: ['SQLite'], answeredVia: 'claude_app' })
  })
})
