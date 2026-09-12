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
    maxActiveSessions: 5,
  },
  managed: [],
  about: { gitSha: null, hostName: 'test-host' },
}

/**
 * One catch-all handler, dispatching on pathname — the `dashboard.spec.ts`
 * pattern. Object-shaped endpoints are matched explicitly; the `[]` fallback is
 * only safe for collections, so anything the roadmap reads as an object gets its
 * own branch (and the `/projects/:slug` sub-resources are matched BEFORE the
 * project itself, since `/deps` and `/onboarding` are suffixes of that path).
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

  test('the list owns the band until a product is open, then the detail does', async ({ page }) => {
    await page.goto('/roadmap')
    // Nothing open at this width: the list has the single content band and the
    // page is `hidden` (attribute, so it leaves the a11y tree too) — hence no
    // `h1` here, unlike desktop.
    const tree = page.getByRole('tree', { name: 'Map' })
    await expect(tree).toBeVisible()
    await expect(page.getByRole('heading', { level: 1 })).toHaveCount(0)
    await shot(page, 'phone-portfolio')

    await page.getByRole('button', { name: /^Open product / }).first().click()
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('KinDash')
    expect(pathOf(page)).toBe('/roadmap/kindash')
    // The band changed hands: the list is gone once something is open.
    await expect(tree).toHaveCount(0)
    await shot(page, 'phone-product')
  })
})
