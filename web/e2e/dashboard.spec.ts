/**
 * T044 — Dashboard E2E tests.
 *
 * Prerequisites:
 *   - `npm run build && npm run preview` (or `npm run dev`) running at http://127.0.0.1:4173
 *   - Browsers installed: `npx playwright install`
 *   - The backend API is intercepted via Playwright's route interception so no real server is needed.
 *
 * Run: npx playwright test e2e/dashboard.spec.ts
 *
 * Viewport note: tests use a mobile viewport (375×812) so that the app takes the
 * routed mobile path (DashboardRoute → /sessions → SessionView) rather than the
 * DesktopShell master-detail layout. On the mobile path:
 *   - The /sessions page renders a top-level <h1>ccswitch</h1>.
 *   - Navigating to /session/:id unmounts the dashboard and mounts SessionView,
 *     which has no h1 but does have "Back to session list" + the Composer.
 * The desktop layout (≥1024px) keeps the h1 persistent and has no "Back" button,
 * so the heading-disappears and back-button assertions only hold on mobile.
 */

import { test, expect, type Page } from '@playwright/test'

// Force the mobile routing branch for all tests in this file.
test.use({ viewport: { width: 375, height: 812 } })

// ── Fixtures ──────────────────────────────────────────────────────────────────

const MOCK_SESSIONS = [
  {
    id: 'sess-1',
    name: 'Work session',
    accountName: 'alice',
    status: 'working',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  },
]

const MOCK_ACCOUNTS = [
  {
    name: 'alice',
    status: 'ok',
    usage: { session: { pct: 30, resetsAt: null }, weekly: { pct: 50, resetsAt: null } },
  },
]

// Intercept every /api/* request so the specs are fully backend-independent.
// A single catch-all route handler dispatches on URL pathname so that query-param
// variants (e.g. /api/sessions?viewing=1 from useSessionsViewing) are handled
// correctly. Collection endpoints return [] rather than {} so that any .find() /
// .map() calls the app makes won't throw a "not a function" error.
async function interceptApi(page: Page) {
  await page.route('**/api/**', (route) => {
    const url = route.request().url()
    const path = new URL(url).pathname

    if (path === '/api/sessions') {
      route.fulfill({ json: MOCK_SESSIONS })
    } else if (path === '/api/accounts') {
      route.fulfill({ json: MOCK_ACCOUNTS })
    } else if (path === '/api/sessions/archived') {
      route.fulfill({ json: [] })
    } else if (path === '/api/models') {
      route.fulfill({ json: [] })
    } else if (path === '/api/settings') {
      route.fulfill({ json: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null, defaultToolDetail: 'condensed' } })
    } else if (path === '/api/config') {
      route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } })
    } else if (path === '/api/daemon/health') {
      route.fulfill({ json: { status: 'ok' } })
    } else if (path === '/api/x11/metrics') {
      route.fulfill({ json: { enabled: false } })
    } else if (path === '/api/deploy/status') {
      route.fulfill({ json: {
        pending: null,
        deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: '' },
      } })
    } else if (path === '/api/automations') {
      route.fulfill({ json: [] })
    } else if (path.startsWith('/api/github')) {
      route.fulfill({ json: [] })
    } else {
      // Generic fallback: empty array is safer than {} for collection endpoints
      // the app might call .find() / .map() on without null-checking.
      route.fulfill({ json: [] })
    }
  })

  // Intercept WebSocket so it doesn't try to connect
  await page.routeWebSocket('**/ws', (ws) => {
    // Accept but stay silent — no messages from server
    ws.onMessage(() => {})
  })
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe('T044 — Dashboard', () => {
  test.beforeEach(async ({ page }) => {
    await interceptApi(page)
  })

  test('dashboard loads with session list', async ({ page }) => {
    await page.goto('/sessions')

    // Heading visible
    await expect(page.getByRole('heading', { name: /ccswitch/i })).toBeVisible()

    // Session list loads. Scope the account-badge check to the session card —
    // the DispatchBar's Account dropdown also renders an "alice" <option>, so a
    // page-wide getByText('alice') trips Playwright's strict mode. The account
    // chip is a SIBLING of the open-session button (interactive chips must not
    // nest inside it), so scope to the card <article> that contains the button.
    await expect(page.getByText('Work session')).toBeVisible()
    const sessionCard = page.locator('article', {
      has: page.getByRole('button', { name: /open session work session/i }),
    })
    await expect(sessionCard.getByText('alice')).toBeVisible()
  })

  test('clicking Open on a session opens the Terminal view', async ({ page }) => {
    await page.goto('/sessions')

    await expect(page.getByText('Work session')).toBeVisible()

    // Click the session open button
    await page.getByRole('button', { name: /open session work session/i }).click()

    // SessionView is mounted: the dashboard h1 is gone, replaced by the session
    // top bar. The "Back to session list" button and the message composer confirm
    // we are in the full-screen terminal view.
    await expect(page.getByRole('heading', { name: /ccswitch/i })).not.toBeVisible()
    await expect(page.getByRole('button', { name: /back to session list/i })).toBeVisible()
    // The Composer textarea is present (confirms the session UI is mounted).
    await expect(page.getByRole('textbox', { name: /message/i })).toBeVisible()
  })

  test('clicking Back returns to the dashboard', async ({ page }) => {
    await page.goto('/sessions')

    await expect(page.getByText('Work session')).toBeVisible()
    await page.getByRole('button', { name: /open session work session/i }).click()

    // Click back
    await page.getByRole('button', { name: /back to session list/i }).click()

    // Dashboard heading visible again
    await expect(page.getByRole('heading', { name: /ccswitch/i })).toBeVisible()
  })

  test('mobile viewport: no horizontal overflow, composer present in session view', async ({ page }) => {
    await page.goto('/sessions')

    // No horizontal overflow
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth)
    const viewportWidth = await page.evaluate(() => window.innerWidth)
    expect(bodyWidth).toBeLessThanOrEqual(viewportWidth)

    // Navigate to terminal view
    await expect(page.getByText('Work session')).toBeVisible()
    await page.getByRole('button', { name: /open session work session/i }).click()

    // The Composer's "Send message" button is at least 44 px tall (touch target spec).
    await expect(page.getByRole('button', { name: /send message/i })).toBeVisible()
  })

  test('mobile: all header tabs are reachable and badges render', async ({ page }) => {
    await page.goto('/sessions')

    // Wait for the shell to settle.
    await expect(page.getByText('Work session')).toBeVisible()

    // All eight sections render as tabs in the strip — nothing is dropped on a
    // narrow viewport. (Accounts/Library live inside Settings now.)
    const tablist = page.getByRole('tablist', { name: /dashboard sections/i })
    await expect(tablist.getByRole('tab')).toHaveCount(8)
    for (const name of [/sessions/i, /pipeline/i, /automations/i, /initiatives/i, /products/i, /archived/i, /insights/i, /settings/i]) {
      await expect(tablist.getByRole('tab', { name })).toHaveCount(1)
    }

    // Count badges follow the "Archived 111" pattern: 1 live session.
    await expect(tablist.getByRole('tab', { name: /sessions \(1\)/i })).toBeVisible()

    // The strip itself scrolls horizontally rather than widening the page, so the
    // body never overflows the viewport.
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth)
    const viewportWidth = await page.evaluate(() => window.innerWidth)
    expect(bodyWidth).toBeLessThanOrEqual(viewportWidth)

    // An off-screen tab (last in the strip) is still reachable: scroll to it,
    // click it, and confirm it activates. This is the core "reachable on mobile"
    // guarantee.
    const settings = tablist.getByRole('tab', { name: /settings/i })
    await settings.scrollIntoViewIfNeeded()
    await settings.click()
    await expect(settings).toHaveAttribute('aria-selected', 'true')

    await page.screenshot({ path: 'test-results/mobile-header-tabs.png' })
  })

  test('session view composer send button is ≥44px tall', async ({ page }) => {
    await page.goto('/sessions')

    await expect(page.getByText('Work session')).toBeVisible()
    await page.getByRole('button', { name: /open session work session/i }).click()

    // The Composer has an explicit min-h-[44px] on the Send button.
    const sendBtn = page.getByRole('button', { name: /send message/i })
    await expect(sendBtn).toBeVisible()

    const height = await sendBtn.evaluate((el) => el.getBoundingClientRect().height)
    expect(height).toBeGreaterThanOrEqual(44)
  })
})
