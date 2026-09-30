/**
 * Roadmap Phase 3 — E2E smoke for the one Roadmap section (finish-line spec §4,
 * roadmap drill-down spec §5–§7).
 *
 * Prerequisites (same harness as `dashboard.spec.ts`):
 *   - `npm run build && npm run preview` at http://127.0.0.1:4173 — Playwright's
 *     `webServer` block starts this for you.
 *   - Browsers installed: `npx playwright install chromium`.
 *   - There is NO backend. Every `/api/*` call is intercepted below, so nothing
 *     here can be seeded through a real endpoint; the map fixture is inlined
 *     (a copy of `web/src/features/roadmap/__fixtures__/map.ts`, which the specs
 *     cannot import because Playwright's transform does not resolve the `@/`
 *     Vite alias).
 *
 * Run: npx playwright test e2e/roadmap.spec.ts
 *
 * Screenshots: set `ROADMAP_SHOTS=1` to write the review shots under
 * `.superpowers/sdd/2026-09-08-roadmap-phase3-products-master-detail/shots/`.
 * They are off by default so CI never writes files outside `test-results/`.
 */

import { test, expect, type Page } from '@playwright/test'

const SHOTS = !!process.env.ROADMAP_SHOTS
const SHOT_DIR = '../.superpowers/sdd/2026-09-08-roadmap-phase3-products-master-detail/shots'

/** Write a review screenshot, but only when explicitly asked for. */
async function shot(page: Page, name: string) {
  if (!SHOTS) return
  await page.screenshot({ path: `${SHOT_DIR}/${name}.png`, fullPage: false })
}

// ── Fixtures ──────────────────────────────────────────────────────────────────

interface CapOver { slug: string; [k: string]: unknown }

const cap = (over: CapOver) => ({
  id: `f-${over.slug}`,
  title: over.slug,
  state: 'live',
  summary: null,
  docPath: null,
  commitSha: null,
  prNumber: null,
  parentSlug: null,
  tier: 'idle',
  counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 },
  links: [],
  initiatives: [],
  personaSlugs: [],
  children: [],
  ...over,
})

const ini = (over: { slug: string; projectSlug: string; [k: string]: unknown }) => ({
  title: over.slug,
  stage: 'build',
  status: 'active',
  primaryCapability: null,
  capabilities: [],
  tier: 'in-flight',
  reason: null,
  epic: null,
  ...over,
})

const product = (over: CapOver) => ({
  name: over.slug,
  tier: 'idle',
  counts: { inFlight: 0, blocked: 0, needsYou: 0, capabilities: 0, awaitingTriage: 0, needsHome: 0 },
  capabilities: [],
  initiatives: [],
  ...over,
})

/** Mirrors `web/src/features/roadmap/__fixtures__/map.ts`: kindash ▸ tasks ▸
 *  task-views, plus billing; viberpit ▸ sessions. */
const MAP = {
  generatedAt: '2026-09-08T00:00:00Z',
  products: [
    product({
      slug: 'kindash',
      name: 'KinDash',
      tier: 'needs-you',
      counts: { inFlight: 1, blocked: 0, needsYou: 1, capabilities: 3, awaitingTriage: 2, needsHome: 0 },
      capabilities: [
        cap({ slug: 'billing', title: 'Billing' }),
        cap({
          slug: 'tasks',
          title: 'Tasks',
          tier: 'in-flight',
          counts: { openIssues: 5, inFlight: 0, blocked: 0, needsYou: 0 },
          children: [
            cap({
              slug: 'task-views',
              title: 'Task views',
              parentSlug: 'tasks',
              tier: 'in-flight',
              counts: { openIssues: 3, inFlight: 0, blocked: 0, needsYou: 0 },
              initiatives: ['views'],
            }),
          ],
        }),
      ],
      initiatives: [
        ini({
          slug: 'views',
          title: 'Use-case-first task views',
          projectSlug: 'kindash',
          primaryCapability: 'task-views',
          capabilities: ['task-views'],
        }),
        ini({ slug: 'orphan', title: 'No home yet', projectSlug: 'kindash', tier: 'needs-you', reason: 'advance' }),
      ],
    }),
    product({ slug: 'viberpit', name: 'ViberPit', capabilities: [cap({ slug: 'sessions', title: 'Sessions' })] }),
  ],
}

const PROJECT = {
  id: 'p-kindash',
  slug: 'kindash',
  name: 'KinDash',
  description: 'Family logistics',
  status: 'active',
  portfolioTags: [],
  onboardingState: 'live',
  createdAt: '2026-06-01T00:00:00Z',
  updatedAt: '2026-09-08T00:00:00Z',
  repos: [{ repo: 'steiner385/kindash', isPrimary: true }],
  edges: [],
  featureCount: 3,
  workItemCount: 12,
}

const ONBOARDING = {
  state: 'live',
  action: { type: 'live' },
  counts: { repoCount: 1, backfillDoneCount: 1, featureCount: 3, openUnclassifiedCount: 0, pendingProposalCount: 0 },
  mapPr: null,
}

const DEPS = {
  project: { number: 27, title: 'KinDash', lastSyncedAt: '2026-09-08T00:00:00Z' },
  edges: [],
  drift: { proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [] },
}

/** `GET /pm/projects/:slug/health` — the product overview's hygiene notice
 *  (`RoadmapHealthCard`). A CLEAN product: the card renders nothing when there
 *  is nothing to say, so the smoke tests below see the same page they always
 *  did. The card's own rendering is covered by `RoadmapHealthCard.test.tsx`. */
const HEALTH = {
  unparentedCapabilities: [],
  unlinkedInitiatives: [],
  staleProposed: [],
  staleInbox: 0,
  generatedAt: '2026-09-08T00:00:00Z',
}

const INITIATIVE = {
  slug: 'views',
  repo: 'steiner385/kindash',
  title: 'Use-case-first task views',
  stage: 'build',
  status: 'active',
  origin: 'human',
  originRef: null,
  epicRepo: null,
  epicNumber: null,
  createdAt: '2026-08-01T00:00:00Z',
  updatedAt: '2026-09-08T00:00:00Z',
  artifacts: [],
  epic: null,
  projectSlug: 'kindash',
  primaryCapability: { id: 'f-task-views', slug: 'task-views', title: 'Task views', isPrimary: true },
  capabilities: [{ id: 'f-task-views', slug: 'task-views', title: 'Task views', isPrimary: true }],
  buildSessions: [],
  nextAction: { type: 'await_approval', kind: 'plan' },
}

const SETTINGS = {
  defaultPermissionMode: 'interactive',
  thinkingCollapsed: false,
  repoRoot: null,
  defaultToolDetail: 'auto',
  maxArchivedSessions: null,
  archiveHome: null,
  maxActiveSessions: null,
  autoArchiveEnabled: true,
  modelApprovalAction: null,
  modelApprovalDenylist: null,
  acceptedPermissionModes: ['interactive', 'auto', 'allowlist', 'acceptEdits', 'full-auto', 'bypassPermissions'],
  effectiveDefaults: {
    repoRoot: '/home/user/GitHub',
    archiveHome: null,
    modelApprovalAction: 'require_approval',
    modelApprovalDenylist: 'fable',
    globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] },
  },
  managed: [],
  about: { gitSha: null, hostName: 'test-host' },
}

/**
 * One catch-all handler, dispatching on pathname — the `dashboard.spec.ts`
 * pattern. Object-shaped endpoints are matched explicitly; the `[]` fallback is
 * only safe for collections, so anything the roadmap reads as an object gets its
 * own branch (and the `/projects/:slug` sub-resources are matched BEFORE the
 * project itself, since `/deps`, `/onboarding` and `/health` are suffixes of
 * that path — fall through to the project fixture and the caller gets an object
 * of the wrong shape, which is a render crash, not a soft failure).
 */
async function interceptApi(page: Page) {
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname

    if (path === '/api/pm/map') {
      route.fulfill({ json: MAP })
    } else if (path === '/api/pm/queue') {
      route.fulfill({ json: { entries: [], skipped: 0, generatedAt: '2026-09-08T00:00:00Z' } })
    } else if (path === '/api/pm/leaves') {
      route.fulfill({ json: { items: [] } })
    } else if (path.endsWith('/onboarding')) {
      route.fulfill({ json: ONBOARDING })
    } else if (path.endsWith('/deps')) {
      route.fulfill({ json: DEPS })
      // Scoped to the `/api/pm/projects/` prefix, NOT a bare `endsWith('/health')`:
      // `/api/daemon/health` is matched further down this same chain, so an
      // unscoped branch here would hijack it and hand the daemon poller a
      // `ProjectHealth` object.
    } else if (path.startsWith('/api/pm/projects/') && path.endsWith('/health')) {
      route.fulfill({ json: HEALTH })
    } else if (path.startsWith('/api/pm/projects/')) {
      route.fulfill({ json: PROJECT })
    } else if (path === '/api/sdlc/triage') {
      route.fulfill({ json: { items: [] } })
    } else if (path.startsWith('/api/sdlc/initiatives/')) {
      route.fulfill({ json: INITIATIVE })
    } else if (path === '/api/pm/triage') {
      // A bare ARRAY, not `{ items }` — `fetchTriage` returns `TriageProposal[]`
      // and the Inbox calls `.length`/`.find` on it directly.
      route.fulfill({ json: [] })
    } else if (path === '/api/settings') {
      route.fulfill({ json: SETTINGS })
    } else if (path === '/api/config') {
      route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } })
    } else if (path === '/api/daemon/health') {
      route.fulfill({ json: { status: 'ok' } })
    } else if (path === '/api/x11/metrics') {
      route.fulfill({ json: { enabled: false } })
    } else if (path === '/api/deploy/status') {
      route.fulfill({
        json: { pending: null, deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: '' } },
      })
    } else {
      // Collections (sessions, accounts, models, automations, github…).
      route.fulfill({ json: [] })
    }
  })

  await page.routeWebSocket('**/ws', (ws) => {
    ws.onMessage(() => {})
  })
}

const pathOf = (page: Page) => new URL(page.url()).pathname

// ── Desktop (1440×900): the list column is beside the detail ──────────────────

test.describe('Roadmap — desktop', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test.beforeEach(async ({ page }) => {
    await interceptApi(page)
  })

  test('drills from the portfolio to a product and back', async ({ page }) => {
    await page.goto('/roadmap')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Roadmap')

    // The Map scope's tree is the list column; `TreeRowCard` labels a product
    // row's open control "Open product <name>".
    const first = page.getByRole('button', { name: /^Open product / }).first()
    await expect(first).toBeVisible()
    const name = (await first.getAttribute('aria-label'))!.replace('Open product ', '')
    await first.click()

    await expect(page).toHaveURL(/\/roadmap\/[^/?#]+$/)
    expect(pathOf(page)).toBe('/roadmap/kindash')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(name)
    // The list column is still there beside the detail at this width.
    await expect(page.getByRole('tree', { name: 'Map' })).toBeVisible()

    await page.goBack()
    await expect.poll(() => pathOf(page)).toBe('/roadmap')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Roadmap')
  })

  test('a capability under an open map-changes PR badges its row and the product page shows the PR card', async ({ page }) => {
    // Overrides the `beforeEach` intercept for this one route — Playwright tries
    // the most-recently-registered matching handler first, so this wins over
    // `interceptApi`'s `/api/pm/map` branch without touching the shared fixture.
    const kindash = MAP.products[0]
    await page.route('**/api/pm/map', (route) => {
      route.fulfill({
        json: {
          ...MAP,
          products: [
            {
              ...kindash,
              mapChanges: {
                branch: 'pm/map-changes-kindash', prNumber: 31,
                prUrl: 'https://github.com/steiner385/kindash/pull/31', status: 'open',
                openedAt: '2026-09-10T00:00:00Z', headSha: 'abc123', behind: false, conflicting: false,
              },
              capabilities: [
                cap({ slug: 'billing', title: 'Billing', prNumber: 31 }),
                ...kindash.capabilities.slice(1),
              ],
            },
            MAP.products[1],
          ],
        },
      })
    })

    await page.goto('/roadmap')
    await expect(page.getByRole('button', { name: 'Open capability Billing' })).toBeVisible()
    // Scoped to the row that owns the badge (`capabilityKey`), as the vitest
    // case is: the PR number also appears on the product page's card, and an
    // unscoped match would pass on either.
    await expect(page.getByTestId('row-capability:kindash:billing').getByText('PR #31')).toBeVisible()

    await page.goto('/roadmap/kindash')
    // Scoped past `getByRole('status')`: the page also carries a "Loading
    // roadmap" status region and an sr-only global loading announcer.
    await expect(page.getByRole('status').filter({ hasText: 'Capability map changes are open' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'PR #31' })).toBeVisible()
  })

  test('/projects redirects to /roadmap', async ({ page }) => {
    await page.goto('/projects')
    await expect.poll(() => pathOf(page)).toBe('/roadmap')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Roadmap')
  })

  test('review screenshots — portfolio, product, capability, initiative', async ({ page }) => {
    test.skip(!SHOTS, 'set ROADMAP_SHOTS=1 to write review screenshots')

    await page.goto('/roadmap')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Roadmap')
    await shot(page, 'desktop-portfolio')

    await page.goto('/roadmap/kindash')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('KinDash')
    await shot(page, 'desktop-product')

    await page.goto('/roadmap/kindash/capabilities/tasks')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Tasks')
    await shot(page, 'desktop-capability')

    await page.goto('/initiatives/views')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Use-case-first task views')
    await shot(page, 'desktop-initiative')
  })
})

// ── Phone (390×844): the detail takes the single content band ─────────────────

test.describe('Roadmap — phone', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test.beforeEach(async ({ page }) => {
    await interceptApi(page)
  })

  test('the section opens on the portfolio queue; the Map list is one tap away and owns the band until a product is open', async ({ page }) => {
    // Bare `/roadmap` is the PORTFOLIO page, and at this width it holds the
    // band: the whole-roadmap Needs-you queue is the section's front door
    // (PRODUCT.md principle 1). The tree is behind the Back link, at `?scope=map`.
    await page.goto('/roadmap')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Roadmap')
    await expect(page.getByRole('tab', { name: /^Needs you/ })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByRole('tree', { name: 'Map' })).toHaveCount(0)
    await shot(page, 'phone-portfolio')

    await page.getByRole('link', { name: 'Back to Map' }).click()
    expect(pathOf(page)).toBe('/roadmap')
    // A list scope: the list has the single content band and the page is
    // `hidden` (attribute, so it leaves the a11y tree too) — hence no `h1`
    // here, unlike desktop.
    const tree = page.getByRole('tree', { name: 'Map' })
    await expect(tree).toBeVisible()
    await expect(page.getByRole('heading', { level: 1 })).toHaveCount(0)
    await shot(page, 'phone-map')

    await page.getByRole('button', { name: /^Open product / }).first().click()
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('KinDash')
    expect(pathOf(page)).toBe('/roadmap/kindash')
    // The band changed hands: the list is gone once something is open.
    await expect(tree).toHaveCount(0)
    await shot(page, 'phone-product')
  })
})

// Mutable API fixture: assertions below observe real in-app mutation requests,
// rather than considering an external link a completed disposition flow.
async function attentionIssueApi(page: Page) {
  await interceptApi(page)
  let item = {
    id: 'w42', repo: 'steiner385/kindash', number: 42,
    title: 'Clarify remote login recovery',
    body: 'The account usage report is unavailable. **Check authentication** before retrying.',
    state: 'open', url: 'https://github.com/steiner385/kindash/issues/42', version: 'v1',
  }
  const writes: Record<string, unknown>[] = []
  await page.route('**/api/pm/map', (route) => {
    const attention = item.state === 'open' ? 1 : 0
    return route.fulfill({ json: {
      generatedAt: MAP.generatedAt,
      products: [product({
        slug: 'kindash', name: 'KinDash', tier: attention ? 'needs-you' : 'live',
        counts: { inFlight: 0, blocked: 0, needsYou: attention, capabilities: 1, awaitingTriage: 0, needsHome: 0 },
        capabilities: [cap({
          slug: 'account-reauthentication', title: 'Account Re-Authentication', tier: attention ? 'needs-you' : 'live',
          summary: 'Restore an expired account to service.',
          counts: { openIssues: attention, inFlight: 0, blocked: 0, needsYou: attention },
        })],
      })],
    } })
  })
  await page.route('**/api/pm/leaves?*', (route) => route.fulfill({ json: { items: [{
    ...item, ref: 'steiner385/kindash#42', kind: 'issue', triageState: 'needs_human',
    score: null, reach: null, impact: null, effort: null, confidence: null,
    tier: item.state === 'open' ? 'needs-you' : 'live', blockedBy: [], parentRef: null,
    featureId: 'f-account-reauthentication', sessionStatus: null, ghCreatedAt: null,
    reason: null, bounces: 0,
  }] } }))
  await page.route('**/api/pm/work-items/w42/issue', async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: item })
    const body = route.request().postDataJSON() as Record<string, unknown>
    writes.push(body)
    if (body.version !== item.version) return route.fulfill({ status: 409, json: { detail: { message: 'Issue changed. Reload it before deciding.' } } })
    item = {
      ...item,
      ...(body.action === 'edit'
        ? { title: String(body.title), body: String(body.body) }
        : { state: 'closed' }),
      version: `v${writes.length + 1}`,
    }
    return route.fulfill({ json: { ok: true, item } })
  })
  return writes
}

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test.describe(`Roadmap issue disposition — ${viewport.width}px`, () => {
    test.use({ viewport })

    test('reads, edits and resolves an issue without leaving the app', async ({ page }, testInfo) => {
      const writes = await attentionIssueApi(page)
      await page.goto('/roadmap/kindash/capabilities/account-reauthentication')
      await expect(page.getByRole('heading', { name: 'Next actions' })).toBeVisible()
      await page.getByRole('button', { name: 'Needs you: 1', exact: true }).click()
      await page.getByRole('button', { name: 'Review and decide', exact: true }).click()
      const dialog = page.getByRole('dialog', { name: 'Review and decide' })
      await expect(dialog.getByText(/The account usage report is unavailable/)).toBeVisible()
      expect(writes).toHaveLength(0)
      await page.screenshot({ path: testInfo.outputPath('review-decision.png') })
      await dialog.getByRole('button', { name: 'Edit details', exact: true }).click()
      await dialog.getByRole('textbox', { name: 'Description', exact: true }).fill('Confirm the login flow restores quota reporting after authentication.')
      await dialog.getByRole('button', { name: 'Save changes', exact: true }).click()
      await expect.poll(() => writes.length).toBe(1)
      expect(writes[0]).toMatchObject({ action: 'edit', version: 'v1', body: 'Confirm the login flow restores quota reporting after authentication.' })
      await expect(dialog.getByText('Confirm the login flow restores quota reporting after authentication.', { exact: true })).toBeVisible()
      await dialog.getByRole('button', { name: 'Mark resolved', exact: true }).click()
      expect(writes).toHaveLength(1)
      await dialog.getByRole('button', { name: 'Confirm resolved', exact: true }).click()
      await expect.poll(() => writes.length).toBe(2)
      expect(writes[1]).toMatchObject({ action: 'resolve', version: 'v2' })
      await expect(page.getByRole('button', { name: 'Needs you: 0', exact: true })).toBeVisible()
      await expect(page.getByText('Nothing needs you', { exact: true })).toBeVisible()
      expect(pathOf(page)).toBe('/roadmap/kindash/capabilities/account-reauthentication')
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath('resolved.png') })
    })

    test('can cancel or confirm a won’t-do decision', async ({ page }) => {
      const writes = await attentionIssueApi(page)
      await page.goto('/roadmap/kindash/capabilities/account-reauthentication')
      await page.getByRole('button', { name: 'Review what needs you', exact: true }).click()
      await expect(page.getByRole('tab', { name: 'Needs you (1, needs attention)' })).toBeFocused()
      await page.getByRole('button', { name: 'Review and decide', exact: true }).click()
      const dialog = page.getByRole('dialog', { name: 'Review and decide' })
      await dialog.getByRole('button', { name: 'Won’t do', exact: true }).click()
      await dialog.getByRole('button', { name: 'Go back', exact: true }).click()
      expect(writes).toHaveLength(0)
      await dialog.getByRole('button', { name: 'Won’t do', exact: true }).click()
      await dialog.getByRole('button', { name: 'Confirm won’t do', exact: true }).click()
      await expect.poll(() => writes.length).toBe(1)
      expect(writes[0]).toMatchObject({ action: 'dismiss', version: 'v1' })
      await expect(page.getByRole('button', { name: 'Needs you: 0', exact: true })).toBeVisible()
    })
  })
}
