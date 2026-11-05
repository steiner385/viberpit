/**
 * The release-milestones e2e fixture, shared by `milestones.spec.ts` and
 * `milestones-print.spec.ts`: a trimmed inline copy of
 * `web/src/features/milestones/__fixtures__/ladder.ts` (Playwright's transform
 * does not resolve the `@/` alias) — four milestones, the household gate current —
 * and `interceptApi`, which mocks every `/api/*` call (no backend).
 */
import type { Page } from '@playwright/test'

export const ago = (secs: number) => new Date(Date.now() - secs * 1000).toISOString()

export const cond = (over: Record<string, unknown> & { key: string }) => ({
  label: over.key, kind: 'manual', status: 'unknown', value: null, target: null, op: null,
  measuredAt: null, ageSeconds: null, error: null, sourceUrl: null, manualBy: null, manualEvidence: null,
  ...over,
})

export function note(over: Record<string, unknown> & { id: string; kind: string; text: string; status: string }) {
  return { ref: null, url: null, milestoneKey: null, createdBy: 'tony', createdAt: ago(86400), resolvedBy: null, resolvedAt: null, ...over }
}

export const leaves = (done: number, inFlight: number, notStarted: number) => ({
  done, inFlight, notStarted, total: done + inFlight + notStarted, measuredAt: ago(240),
  items: [
    ...Array.from({ length: inFlight }, (_, i) => ({
      ref: `o/r#${100 + i}`, title: `In flight ${i}`, url: `https://github.com/o/r/issues/${100 + i}`,
      state: 'in_flight', prs: [{ number: 200 + i, url: `https://github.com/o/r/pull/${200 + i}` }],
    })),
    ...Array.from({ length: notStarted }, (_, i) => ({
      ref: `o/r#${300 + i}`, title: `Not started ${i}`, url: `https://github.com/o/r/issues/${300 + i}`,
      state: 'not_started', prs: [],
    })),
    ...Array.from({ length: done }, (_, i) => ({
      ref: `o/r#${400 + i}`, title: `Done ${i}`, url: `https://github.com/o/r/issues/${400 + i}`,
      state: 'done', prs: [],
    })),
  ],
})

export const milestone = (key: string, label: string, name: string, state: string, over: Record<string, unknown> = {}) => ({
  key, label, name, audience: `${name} audience`,
  platforms: [{ name: 'Web', off: false, note: null }, { name: 'iOS', off: true, note: 'not yet' }],
  state, enteredAt: state === 'current' ? '2026-10-01T00:00:00Z' : null, exitedAt: null,
  statusNote: null, exitOverride: null,
  epics: [{ ref: `#${key}`, url: `https://github.com/o/r/issues/1` }],
  gate: [cond({ key: 'manual', label: 'A manual check' })],
  gateSummary: { met: 0, total: 1, unknown: 1, ready: false },
  leaves: leaves(0, 0, 0),
  ...over,
})

export const LADDER = {
  ladder: {
    title: 'Launch readiness', specPath: null, specUrl: null,
    sourcePath: 'docs/product/milestones.yaml', sourceSha: 'abc1234', syncedAt: ago(600),
    parseError: null, parseErrorSha: null, currentKey: 'household', currentRemoved: false,
    updatedAt: ago(240), autoRefresh: true, ladderComplete: false, asksOpen: 1,
  },
  milestones: [
    milestone('household', 'Phase 0', 'Household gate', 'current', {
      statusNote: 'the long phase by design',
      gate: [
        cond({ key: 'rubric', label: 'Evidence rubric satisfied', kind: 'ci_metric', status: 'unmet', value: { n: 293 }, op: 'eq', target: 0, measuredAt: ago(3 * 3600) }),
        cond({ key: 'lane-fixes', label: 'Nine lane fixes landed', kind: 'refs_closed', status: 'unmet', value: { done: 8, total: 9 }, measuredAt: ago(240) }),
        cond({ key: 'nightly', label: 'Nightly green three runs', kind: 'workflow_streak', status: 'unknown', value: { n: 2 }, op: 'ge', target: 3, measuredAt: ago(37 * 3600), error: 'GitHub returned 502' }),
        cond({ key: 'consent', label: 'Consent captured', kind: 'refs_closed', status: 'met', value: { done: 3, total: 3 }, measuredAt: ago(240) }),
      ],
      gateSummary: { met: 1, total: 4, unknown: 1, ready: false },
      leaves: leaves(17, 1, 0),
    }),
    milestone('friendly', 'Phase 1', 'Friendly families', 'upcoming', { leaves: leaves(0, 1, 8) }),
    milestone('community', 'Phase 2', 'Community cohort', 'upcoming'),
    milestone('public', 'Phase 3', 'Public self-serve', 'upcoming'),
  ],
  numbers: [
    cond({ key: 'rubric-gap', label: 'evidence cells missing', kind: 'ci_metric', status: 'info', value: { n: 293 }, measuredAt: ago(3 * 3600) }),
  ],
  notes: {
    decisions: [note({ id: 'd1', kind: 'decision', ref: 'D1', text: 'Lifetime free PLUS for friendly families', status: 'locked' })],
    asks: [note({ id: 'a1', kind: 'ask', text: 'Confirm the DPA with the payment processor', status: 'open', milestoneKey: 'household' })],
  },
}

export type View = typeof LADDER

/** The view after a write: a deep copy with `edit` applied, as the server answers. */
export function after(edit: (v: View) => void): View {
  const v = structuredClone(LADDER)
  edit(v)
  return v
}

export const PROJECT = {
  id: 'p-kindash', slug: 'kindash', name: 'KinDash', description: null, status: 'active',
  portfolioTags: [], onboardingState: 'live', createdAt: null, updatedAt: null,
  repos: [{ id: 'r1', repo: 'o/r', isPrimary: true, backfillDone: true }], edges: [],
  featureCount: 0, workItemCount: 0,
}

export type Write = { method: string; path: string; status?: number; json: unknown }

/** Mocks every `/api/*` read; `writes` answer the milestone write routes and
 *  record each request body in `seen`. */
export async function interceptApi(page: Page, ladder: unknown = LADDER, writes: Write[] = [], seen: unknown[] = []) {
  await page.route('**/api/**', (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const write = writes.find((w) => w.method === req.method() && w.path === path)
    if (write) {
      seen.push(req.postDataJSON())
      route.fulfill({ status: write.status ?? 200, json: write.json })
    } else if (path === '/api/pm/projects/kindash/milestones') {
      route.fulfill({ json: ladder })
    } else if (path === '/api/pm/projects/kindash') {
      route.fulfill({ json: PROJECT })
    } else if (path === '/api/settings') {
      route.fulfill({ json: { acceptedPermissionModes: [], effectiveDefaults: {}, managed: [], about: { gitSha: null, hostName: 't' } } })
    } else if (path === '/api/config') {
      route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } })
    } else if (path === '/api/daemon/health') {
      route.fulfill({ json: { status: 'ok' } })
    } else if (path === '/api/x11/metrics') {
      route.fulfill({ json: { enabled: false } })
    } else if (path === '/api/deploy/status') {
      route.fulfill({ json: { pending: null, deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: '' } } })
    } else if (path === '/api/pm/map') {
      route.fulfill({ json: { generatedAt: '', products: [] } })
    } else if (path === '/api/pm/holds') {
      route.fulfill({ json: { products: [], held: [], truncated: 0, generatedAt: '' } })
    } else {
      route.fulfill({ json: [] })
    }
  })
  await page.routeWebSocket('**/ws', (ws) => { ws.onMessage(() => {}) })
}
