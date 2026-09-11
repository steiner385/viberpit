/**
 * Finish-line row 5, task 6 — E2E smoke for the Pipeline as the first
 * Workspace-variant page (page-migration spec §2.2, §3 PR 10; shell spec §5).
 *
 * Prerequisites (same harness as `delivery.spec.ts`):
 *   - `npm run build && npm run preview` at http://127.0.0.1:4173 — Playwright's
 *     `webServer` block starts this for you.
 *   - Browsers installed: `npx playwright install chromium`.
 *   - There is NO backend. Every `/api/*` call is intercepted below. The board is
 *     fed one dashboard frame through `/api/pipeline/snapshot` (the fast-paint
 *     read `useDashboard` makes) and the same frame on the `/events` stream.
 *
 * Run: npx playwright test e2e/pipeline.spec.ts
 */

import { test, expect, type Page } from '@playwright/test'

// ── Fixtures ──────────────────────────────────────────────────────────────────

/** Two tracked repos, no PRs: enough for the strip, the band, the tabs and the
 *  per-repo sections; PR rows have their own vitest coverage. */
const FRAME = {
  generatedAt: '2026-09-11T10:00:00Z',
  staleSince: null,
  repos: [
    { repo: 'o/a', hasDeploy: false, prs: [], queue: null },
    { repo: 'o/b', hasDeploy: false, prs: [], queue: null },
  ],
}

const SETTINGS = {
  defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null, defaultToolDetail: 'auto',
  maxArchivedSessions: null, archiveHome: null, maxActiveSessions: null, autoArchiveEnabled: true,
  modelApprovalAction: null, modelApprovalDenylist: null,
  acceptedPermissionModes: ['interactive', 'auto', 'allowlist', 'acceptEdits', 'full-auto', 'bypassPermissions'],
  effectiveDefaults: { repoRoot: '/home/user/GitHub', archiveHome: null, modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable', maxActiveSessions: 5 },
  managed: [], about: { gitSha: null, hostName: 'test-host' },
}

async function interceptApi(page: Page) {
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/pipeline/snapshot') {
      route.fulfill({ json: FRAME })
    } else if (path === '/api/pipeline/events') {
      // One frame, then the stream ends; EventSource reconnects and gets the
      // same frame again, which is harmless for a smoke.
      route.fulfill({ contentType: 'text/event-stream', body: `data: ${JSON.stringify(FRAME)}\n\n` })
    } else if (path === '/api/pipeline/repos') {
      route.fulfill({ json: {} })
    } else if (path === '/api/settings') {
      route.fulfill({ json: SETTINGS })
    } else if (path === '/api/config') {
      route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } })
    } else if (path === '/api/daemon/health') {
      route.fulfill({ json: { status: 'ok' } })
    } else if (path === '/api/x11/metrics') {
      route.fulfill({ json: { enabled: false } })
    } else if (path === '/api/deploy/status') {
      route.fulfill({ json: { pending: null, deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: '' } } })
    } else if (path === '/api/fleet/hosts') {
      route.fulfill({ json: { hosts: [] } })
    } else {
      route.fulfill({ json: [] })
    }
  })
  await page.routeWebSocket('**/ws', (ws) => { ws.onMessage(() => {}) })
}

const noHorizontalOverflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)

// ── Desktop: the Workspace page, its tabs, header actions and legend ─────────

test.describe('Pipeline — desktop', () => {
  test.use({ viewport: { width: 1440, height: 900 } })
  test.beforeEach(async ({ page }) => { await interceptApi(page) })

  test('is the Workspace page: h1, repo tabs bound to ?pipeline=, legend dialog', async ({ page }) => {
    await page.goto('/pipeline')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Pipeline')
    await expect(page.locator('[data-page-variant="workspace"]')).toBeVisible()

    const tabs = page.getByRole('tablist', { name: 'Pipeline repository' })
    await expect(tabs.getByRole('tab')).toHaveCount(2)
    await expect(tabs.getByRole('tab', { name: 'o/a' })).toHaveAttribute('aria-selected', 'true')
    await tabs.getByRole('tab', { name: 'o/b' }).click()
    await expect(tabs.getByRole('tab', { name: 'o/b' })).toHaveAttribute('aria-selected', 'true')
    expect(new URL(page.url()).searchParams.get('pipeline')).toBe('o/b')
    // The board leads with the focused repo.
    await expect(page.getByRole('heading', { level: 3 }).first()).toHaveText(/o\/b/)

    // The health band is header meta; the legend is a header action.
    await expect(page.getByRole('group', { name: 'Overall CI health' })).toBeVisible()
    const legend = page.getByRole('button', { name: 'Legend' })
    await legend.click()
    await expect(page.getByRole('dialog', { name: 'Legend' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog', { name: 'Legend' })).toHaveCount(0)
    await expect(legend).toBeFocused()
    expect(await noHorizontalOverflow(page)).toBe(true)
  })
})

// ── Phone and tablet: the same page, no horizontal scroll ────────────────────

for (const [name, viewport] of [['phone', { width: 390, height: 844 }], ['tablet', { width: 820, height: 1180 }]] as const) {
  test.describe(`Pipeline — ${name}`, () => {
    test.use({ viewport })
    test.beforeEach(async ({ page }) => { await interceptApi(page) })

    test('renders the same page without horizontal overflow', async ({ page }) => {
      await page.goto('/pipeline')
      await expect(page.getByRole('heading', { level: 1 })).toHaveText('Pipeline')
      await expect(page.getByRole('tablist', { name: 'Pipeline repository' })).toBeVisible()
      await expect(page.getByRole('group', { name: 'Status overview' })).toBeVisible()
      expect(await noHorizontalOverflow(page)).toBe(true)
    })
  })
}

// ── Kiosk: read-only, no chrome, the focused repo cycles ─────────────────────

test.describe('Pipeline — kiosk', () => {
  test.use({ viewport: { width: 1440, height: 900 } })
  test.beforeEach(async ({ page }) => { await interceptApi(page) })

  test('hides the tabs and header actions, tiles are not buttons, and the repo cycles', async ({ page }) => {
    test.setTimeout(40_000)
    await page.goto('/pipeline?kiosk=1&cycle=10')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Pipeline')
    await expect(page.getByRole('tablist')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Legend' })).toHaveCount(0)
    const strip = page.getByRole('group', { name: 'Status overview' })
    await expect(strip).toBeVisible()
    await expect(strip.getByRole('button')).toHaveCount(0)
    await expect(page.getByRole('heading', { level: 3 }).first()).toHaveText(/o\/a/)
    // `cycle=10` → the focused repo advances after ten seconds of real time.
    await expect(page.getByRole('heading', { level: 3 }).first()).toHaveText(/o\/b/, { timeout: 15_000 })
    // The kiosk URL is its configuration: the cycle never writes ?pipeline=.
    expect(new URL(page.url()).searchParams.get('pipeline')).toBeNull()
  })
})
