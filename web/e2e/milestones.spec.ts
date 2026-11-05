/**
 * Release milestones (spec 2026-11-05 §9) — E2E for the page at
 * `/roadmap/:productSlug/milestones`: the read-only ladder, ticking a manual
 * condition, and the override dialog round trip (writes answer with the full view).
 *
 * Same harness as `roadmap.spec.ts`: `npm run build && npm run preview`
 * (Playwright's `webServer` starts it), no backend, every `/api/*` call
 * intercepted. The ladder fixture is a trimmed inline copy of
 * `web/src/features/milestones/__fixtures__/ladder.ts` (Playwright's transform
 * does not resolve the `@/` alias): four milestones, the household gate current.
 *
 * Run: npx playwright test e2e/milestones.spec.ts
 */
import { test, expect, type Page } from '@playwright/test'

const ago = (secs: number) => new Date(Date.now() - secs * 1000).toISOString()

const cond = (over: Record<string, unknown> & { key: string }) => ({
  label: over.key, kind: 'manual', status: 'unknown', value: null, target: null, op: null,
  measuredAt: null, ageSeconds: null, error: null, sourceUrl: null, manualBy: null, manualEvidence: null,
  ...over,
})

function note(over: Record<string, unknown> & { id: string; kind: string; text: string; status: string }) {
  return { ref: null, url: null, milestoneKey: null, createdBy: 'tony', createdAt: ago(86400), resolvedBy: null, resolvedAt: null, ...over }
}

const leaves = (done: number, inFlight: number, notStarted: number) => ({
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

const milestone = (key: string, label: string, name: string, state: string, over: Record<string, unknown> = {}) => ({
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

const LADDER = {
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

type View = typeof LADDER

/** The view after a write: a deep copy with `edit` applied, as the server answers. */
function after(edit: (v: View) => void): View {
  const v = structuredClone(LADDER)
  edit(v)
  return v
}

const PROJECT = {
  id: 'p-kindash', slug: 'kindash', name: 'KinDash', description: null, status: 'active',
  portfolioTags: [], onboardingState: 'live', createdAt: null, updatedAt: null,
  repos: [{ id: 'r1', repo: 'o/r', isPrimary: true, backfillDone: true }], edges: [],
  featureCount: 0, workItemCount: 0,
}

type Write = { method: string; path: string; status?: number; json: unknown }

/** Mocks every `/api/*` read; `writes` answer the milestone write routes and
 *  record each request body in `seen`. */
async function interceptApi(page: Page, ladder: unknown = LADDER, writes: Write[] = [], seen: unknown[] = []) {
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

for (const [label, width] of [['desktop', 1440], ['phone', 390]] as const) {
  test.describe(`Milestones — ${label}`, () => {
    test.use({ viewport: { width, height: 900 } })

    test('renders the header strip and four cards, the current one marked', async ({ page }) => {
      await interceptApi(page)
      await page.goto('/roadmap/kindash/milestones')
      await expect(page.getByRole('heading', { level: 1 })).toHaveText('Milestones')
      const strip = page.getByRole('region', { name: 'Where we are' })
      await expect(strip).toContainText('You are here: Phase 0 · Household gate')
      await expect(strip).toContainText('1 of 4 gate conditions met · 1 unknown')
      await expect(strip).toContainText('1 in flight')
      const cards = page.getByRole('list', { name: 'Milestones' }).locator(':scope > li')
      await expect(cards).toHaveCount(4)
      await expect(cards.first()).toHaveAttribute('aria-current', 'step')
      await expect(cards.first()).toContainText('293 → 0')
      // Nothing scrolls sideways at either width.
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
      expect(overflow).toBeLessThanOrEqual(0)
    })
  })
}

test('no ladder: the EmptyState names the definition file', async ({ page }) => {
  await interceptApi(page, { ladder: null })
  await page.goto('/roadmap/kindash/milestones')
  await expect(page.getByText('docs/product/milestones.yaml')).toBeVisible()
})

test.describe('Milestones — manage', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('ticks a manual condition with evidence; the answer replaces the view', async ({ page }) => {
    const seen: unknown[] = []
    const ticked = after((v) => {
      Object.assign(v.milestones[1].gate[0], {
        status: 'met', value: { met: true }, manualBy: 'tony', manualEvidence: 'call notes 11/04', measuredAt: ago(1),
      })
    })
    await interceptApi(page, LADDER, [
      { method: 'PUT', path: '/api/pm/projects/kindash/milestones/friendly/conditions/manual', json: ticked },
    ], seen)
    await page.goto('/roadmap/kindash/milestones')
    const friendly = page.getByRole('listitem', { name: /Phase 1/ })
    await friendly.getByRole('button', { name: 'Tick: A manual check' }).click()
    const dialog = page.getByRole('dialog', { name: 'Tick condition' })
    await dialog.getByLabel(/Reading/).selectOption('met')
    await dialog.getByLabel(/Evidence/).fill('call notes 11/04')
    await dialog.getByRole('button', { name: 'Save tick' }).click()
    await expect(dialog).toBeHidden()
    expect(seen).toEqual([{ met: true, evidence: 'call notes 11/04', url: null }])
    await expect(friendly).toContainText('by tony · call notes 11/04')
    await expect(friendly.getByRole('button', { name: 'Update: A manual check' })).toBeVisible()
  })

  test('override dialog round trip: lists what is unmet, needs a reason, advances', async ({ page }) => {
    const seen: unknown[] = []
    const advanced = after((v) => {
      v.ladder.currentKey = 'friendly'
      Object.assign(v.milestones[0], {
        state: 'done', exitedAt: ago(1),
        exitOverride: { by: 'tony', reason: 'owner accepted the nightly risk', at: ago(1), unmet: [] },
      })
      v.milestones[1].state = 'current'
    })
    await interceptApi(page, LADDER, [
      { method: 'POST', path: '/api/pm/projects/kindash/milestones/advance', json: advanced },
    ], seen)
    await page.goto('/roadmap/kindash/milestones')
    await page.getByRole('button', { name: 'Advance with override…' }).click()
    const dialog = page.getByRole('dialog', { name: 'Advance with override' })
    await expect(dialog.getByRole('list', { name: 'Not met yet' }).getByRole('listitem')).toHaveCount(3)
    const submit = dialog.getByRole('button', { name: 'Advance with override' })
    await dialog.getByLabel(/Reason/).fill('too short')
    await expect(submit).toBeDisabled()
    await dialog.getByLabel(/Reason/).fill('owner accepted the nightly risk')
    await submit.click()
    await expect(dialog).toBeHidden()
    expect(seen).toEqual([{ expectedCurrent: 'household', overrideReason: 'owner accepted the nightly risk' }])
    await expect(page.getByText('Advanced by override: owner accepted the nightly risk')).toBeVisible()
    await expect(page.getByRole('listitem', { name: /Phase 1/ })).toHaveAttribute('aria-current', 'step')
  })
})

test.describe('Milestones — manage on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('a refused override says only the owner can override; the dialog fits the screen', async ({ page }) => {
    await interceptApi(page, LADDER, [
      { method: 'POST', path: '/api/pm/projects/kindash/milestones/advance', status: 403,
        json: { detail: { error: 'override_requires_owner' } } },
    ])
    await page.goto('/roadmap/kindash/milestones')
    await page.getByRole('button', { name: 'Advance with override…' }).click()
    const dialog = page.getByRole('dialog', { name: 'Advance with override' })
    await dialog.getByLabel(/Reason/).fill('owner accepted the nightly risk')
    await dialog.getByRole('button', { name: 'Advance with override' }).click()
    await expect(dialog.getByRole('alert')).toHaveText('Only the owner can override from the ViberPit app')
    const box = await dialog.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(390)
  })
})
