// A `GET /api/pm/projects/:slug/milestones` payload shaped from the KinDash
// ladder (`server/tests/fixtures/milestones/ladder.yaml`): four milestones,
// the household gate current. `web/e2e/fixtures/milestones.ts` inlines a copy
// (Playwright cannot resolve the `@/` alias). Times are relative to load so
// relative-age labels are deterministic enough for the assertions.
import type { Milestone, MilestoneCondition, MilestoneNote, MilestoneNotes, MilestonesView } from '@/api/milestones'

const H = 3600
export const ago = (secs: number) => new Date(Date.now() - secs * 1000).toISOString()

export function cond(over: Partial<MilestoneCondition> & { key: string }): MilestoneCondition {
  return {
    label: over.key,
    kind: 'manual',
    status: 'unknown',
    value: null,
    target: null,
    op: null,
    measuredAt: null,
    ageSeconds: null,
    error: null,
    sourceUrl: null,
    manualBy: null,
    manualEvidence: null,
    manualUrl: null,
    ...over,
  }
}

const leaf = (n: number, state: 'done' | 'in_flight' | 'not_started', prs: number[] = []) => ({
  ref: `cairnea/KinDash#${n}`,
  title: `Leaf ${n}`,
  url: `https://github.com/cairnea/KinDash/issues/${n}`,
  state,
  prs: prs.map((p) => ({ number: p, url: `https://github.com/cairnea/KinDash/pull/${p}` })),
})

const emptyLeaves = { done: 0, inFlight: 0, notStarted: 0, total: 0, measuredAt: null, items: [] }

export const HOUSEHOLD: Milestone = {
  key: 'household',
  label: 'Phase 0',
  name: 'Household gate',
  audience: "The owner's family, onboarded and in daily use",
  platforms: ['Web', 'PWA', 'Kiosk wall', 'Android (Play)'].map((name) => ({ name, off: false, note: null })),
  state: 'current',
  enteredAt: '2026-10-01T00:00:00Z',
  exitedAt: null,
  statusNote: 'the long phase by design',
  exitOverride: null,
  epics: [{ ref: '#20191', url: 'https://github.com/cairnea/KinDash/issues/20191' }],
  gate: [
    cond({
      key: 'rubric', label: 'Evidence rubric satisfied for every live capability', kind: 'ci_metric',
      status: 'unmet', value: { n: 293 }, op: 'eq', target: 0, measuredAt: ago(3 * H), ageSeconds: 3 * H,
      sourceUrl: 'https://github.com/cairnea/KinDash/actions/runs/1',
    }),
    cond({
      key: 'lane-fixes', label: 'Nine lane-integrity fixes landed', kind: 'refs_closed',
      status: 'unmet', value: { done: 8, total: 9 }, measuredAt: ago(240), ageSeconds: 240,
    }),
    cond({
      key: 'nightly', label: 'Nightly full suite green three runs running', kind: 'workflow_streak',
      status: 'unknown', value: { n: 2 }, op: 'ge', target: 3, measuredAt: ago(37 * H), ageSeconds: 37 * H,
      error: 'GitHub returned 502 for nightly.yml',
    }),
    cond({
      key: 'consent', label: 'Card-consent charge captured; processor DPAs on file', kind: 'refs_closed',
      status: 'met', value: { done: 3, total: 3 }, measuredAt: ago(240), ageSeconds: 240,
    }),
  ],
  gateSummary: { met: 1, total: 4, unknown: 1, ready: false },
  leaves: {
    done: 17, inFlight: 1, notStarted: 0, total: 18, measuredAt: ago(240),
    items: [
      leaf(20200, 'in_flight', [20310]),
      ...Array.from({ length: 17 }, (_, i) => leaf(20210 + i, 'done')),
    ],
  },
}

export const FRIENDLY: Milestone = {
  key: 'friendly',
  label: 'Phase 1',
  name: 'Friendly families',
  audience: '3 to 5 households the owner names, lifetime free PLUS',
  platforms: [
    { name: 'Web', off: false, note: null },
    { name: 'PWA', off: false, note: null },
    { name: 'Android', off: false, note: null },
    { name: 'Kiosk', off: true, note: 'not yet' },
  ],
  state: 'upcoming',
  enteredAt: null,
  exitedAt: null,
  statusNote: null,
  exitOverride: null,
  epics: [{ ref: '#20192', url: 'https://github.com/cairnea/KinDash/issues/20192' }],
  gate: [
    cond({ key: 'setup', label: 'Every family completes setup with no support call' }),
    cond({
      key: 'p0', label: 'Zero open P0', kind: 'issue_count', status: 'met', value: { n: 0 }, op: 'eq', target: 0,
      measuredAt: ago(240), ageSeconds: 240,
    }),
  ],
  gateSummary: { met: 1, total: 2, unknown: 1, ready: false },
  leaves: {
    done: 0, inFlight: 1, notStarted: 8, total: 9, measuredAt: ago(240),
    items: [
      ...Array.from({ length: 8 }, (_, i) => leaf(20400 + i, 'not_started')),
      leaf(20450, 'in_flight', [20460]),
    ],
  },
}

export const COMMUNITY: Milestone = {
  ...FRIENDLY,
  key: 'community',
  label: 'Phase 2',
  name: 'Community cohort',
  audience: 'One ND-parent community, batches of ≤25, Plus checkout live at founder pricing',
  platforms: [{ name: 'Web', off: false, note: null }],
  epics: [{ ref: '#20193', url: 'https://github.com/cairnea/KinDash/issues/20193' }],
  gate: [cond({ key: 'paying', label: 'About 20 paying families; trial-to-paid ≥ 12%' })],
  gateSummary: { met: 0, total: 1, unknown: 1, ready: false },
  leaves: emptyLeaves,
}

export const PUBLIC: Milestone = {
  ...FRIENDLY,
  key: 'public',
  label: 'Phase 3',
  name: 'Public self-serve',
  audience: 'Anyone in the US',
  platforms: [
    { name: 'All of the above', off: false, note: null },
    { name: 'iOS', off: true, note: 'after launch, funded by revenue' },
  ],
  epics: [{ ref: '#20194', url: 'https://github.com/cairnea/KinDash/issues/20194' }],
  gate: [cond({ key: 'daily-watch', label: 'Two weeks of daily watch' })],
  gateSummary: { met: 0, total: 1, unknown: 1, ready: false },
  leaves: emptyLeaves,
}

export const NUMBERS: MilestoneCondition[] = [
  cond({ key: 'rubric-gap', label: 'evidence cells missing in live capabilities', kind: 'ci_metric', status: 'info', value: { n: 293 }, measuredAt: ago(3 * H) }),
  cond({ key: 'leaves', label: 'phase leaves closed', kind: 'ladder_leaves', status: 'info', value: { done: 17, total: 27 }, measuredAt: ago(240) }),
  cond({ key: 'outside-families', label: 'outside families on production', status: 'unknown' }),
  cond({ key: 'defects-by-ci', label: 'of recent defects found by CI (%)', status: 'info', value: { n: 40 }, measuredAt: ago(2 * 86400) }),
]

export function note(over: Partial<MilestoneNote> & Pick<MilestoneNote, 'id' | 'kind' | 'text'>): MilestoneNote {
  return {
    ref: null,
    url: null,
    status: over.kind === 'ask' ? 'open' : 'locked',
    milestoneKey: null,
    createdBy: 'tony@example.com',
    createdAt: ago(86400),
    resolvedBy: null,
    resolvedAt: null,
    ...over,
  }
}

/** Server order (`milestone_view._notes`): decisions locked, proposed, superseded;
 *  asks open first, each oldest first. One ask is ladder-wide, one on the current rung. */
export const NOTES: MilestoneNotes = {
  decisions: [
    note({ id: 'd1', kind: 'decision', ref: 'D1', text: 'Lifetime free PLUS for friendly families', createdAt: ago(9 * 86400) }),
    note({ id: 'd2', kind: 'decision', ref: 'D2', status: 'proposed', text: 'Founder pricing at $4/month', url: 'https://github.com/cairnea/KinDash/issues/20300' }),
    note({ id: 'd0', kind: 'decision', ref: 'D0', status: 'superseded', text: 'Charge from day one' }),
  ],
  asks: [
    note({ id: 'a1', kind: 'ask', text: 'Name the 3 to 5 friendly families', createdAt: ago(3 * 86400) }),
    note({ id: 'a2', kind: 'ask', ref: 'Q2', text: 'Confirm the DPA with the payment processor', milestoneKey: 'household' }),
    note({ id: 'a3', kind: 'ask', status: 'done', text: 'Pick a launch city', resolvedBy: 'tony@example.com', resolvedAt: ago(3600) }),
  ],
}

type LoadedLadder = Extract<MilestonesView, { milestones: Milestone[] }>

export function ladderView(over: Partial<LoadedLadder['ladder']> = {}, view: Partial<Omit<LoadedLadder, 'ladder'>> = {}): LoadedLadder {
  return {
    ladder: {
      title: 'Launch readiness',
      specPath: 'docs/sdlc/launch-readiness/spec.md',
      specUrl: 'https://github.com/cairnea/KinDash/blob/HEAD/docs/sdlc/launch-readiness/spec.md',
      sourcePath: 'docs/product/milestones.yaml',
      sourceSha: 'abc1234def5678',
      syncedAt: ago(600),
      parseError: null,
      parseErrorSha: null,
      currentKey: 'household',
      currentRemoved: false,
      updatedAt: ago(240),
      autoRefresh: true,
      ladderComplete: false,
      asksOpen: 2,
      ...over,
    },
    milestones: [HOUSEHOLD, FRIENDLY, COMMUNITY, PUBLIC],
    numbers: NUMBERS,
    notes: NOTES,
    ...view,
  }
}
