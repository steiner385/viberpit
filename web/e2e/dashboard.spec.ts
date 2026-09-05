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
 * ## Viewport note — a LAYOUT MODE, not a code path
 *
 * The 375×812 viewport used to select which SHELL rendered: below 1024px `App`
 * mounted `Dashboard` (a routed, full-screen mobile UI) and at or above it
 * `DesktopShell` (master-detail, `useState` navigation). Two components, two sets
 * of affordances — which is why the assertions below used to hold only on mobile
 * and the header comment had to spell out the differences.
 *
 * There is one shell now (`AppShell`) and one route table. The viewport selects a
 * layout MODE — which regions the shell places, per the visibility matrix — and
 * nothing else. At this width that means: the phone rails (`TopBar` + `NavBar`)
 * instead of the left `NavRail`, and the single content band given to EITHER the
 * session list or the open session, never both. The routes, the pages and the
 * controls are identical at every width.
 *
 * What that changes here:
 *   - There is no `<h1>ccswitch</h1>`. `TopBar` renders the brand as plain text
 *     precisely so the page's own `PageHeader` owns the only `h1`.
 *   - Sections are a `nav` of links ("Sections"), not a `tablist` of tabs. The
 *     phone bar shows the first four and puts the rest behind "More".
 *   - The session pane and the list are still mutually exclusive at this width,
 *     so "open a session ⇒ the list is gone" still holds — it is now a property
 *     of `REGION_VISIBILITY`, not of which component mounted.
 *
 * The four-viewport project matrix is PR 10; this file stays single-viewport.
 */

import { test, expect, type Page } from '@playwright/test'

// Phone layout mode for every test in this file.
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
      // The FULL payload, mirroring `web/src/test/factories.ts::appSettings` — the
      // Settings sections read `effectiveDefaults`, `acceptedPermissionModes`,
      // `managed` and `about`, and a four-field stub renders them against
      // `undefined`. `defaultToolDetail` is one of auto | all-open | all-closed;
      // the old 'condensed' was never a value the app accepts.
      route.fulfill({ json: {
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
      } })
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

    // The phone shell is up: brand in the top rail, sections in the bottom one.
    // (Plain text, not a heading — see the viewport note.)
    await expect(page.getByText('ccswitch', { exact: true })).toBeVisible()
    await expect(page.getByRole('navigation', { name: /sections/i })).toBeVisible()

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

    // The session pane is mounted and the list has given up the content band
    // (`app.list` is dropped once an item is open, at this width). The "Back to
    // session list" control and the composer confirm the session UI is up.
    await expect(page.getByRole('button', { name: /open session work session/i })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /back to session list/i })).toBeVisible()
    // The composer is the dock's, sharing the pane's runtime.
    await expect(page.getByRole('textbox', { name: /message/i })).toBeVisible()
  })

  test('clicking Back returns to the dashboard', async ({ page }) => {
    await page.goto('/sessions')

    await expect(page.getByText('Work session')).toBeVisible()
    await page.getByRole('button', { name: /open session work session/i }).click()

    // Click back
    await page.getByRole('button', { name: /back to session list/i }).click()

    // The list has the band again.
    await expect(page.getByRole('button', { name: /open session work session/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /back to session list/i })).toHaveCount(0)
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

  test('mobile: every section is reachable and badges render', async ({ page }) => {
    await page.goto('/sessions')

    // Wait for the shell to settle.
    await expect(page.getByText('Work session')).toBeVisible()

    // The phone bar shows the first four sections plus More; the rest live in
    // the More sheet. Nothing is dropped — the assertion below reaches one of
    // them — but the bar deliberately does not try to fit eight icon+label
    // controls across 375px, which is what the scrolling tab strip used to do.
    const bar = page.getByRole('navigation', { name: /sections/i })
    for (const name of [/^Sessions/, /^Pipeline/, /^Automations/, /^Initiatives/]) {
      await expect(bar.getByRole('link', { name })).toHaveCount(1)
    }
    await expect(bar.getByRole('button', { name: /^More/ })).toBeVisible()

    // The badge convention is unchanged ("Sessions (1)"), and it is in the
    // accessible name whether or not the control shows a label.
    await expect(bar.getByRole('link', { name: /^Sessions \(1\)/ })).toBeVisible()

    // The bar never widens the page.
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth)
    const viewportWidth = await page.evaluate(() => window.innerWidth)
    expect(bodyWidth).toBeLessThanOrEqual(viewportWidth)

    // A section behind More is still reachable: open the sheet, click it, and
    // confirm the app navigated. This is the core "reachable on mobile"
    // guarantee the tab-strip scroll test used to make.
    await bar.getByRole('button', { name: /^More/ }).click()
    await page.getByRole('link', { name: /^Settings/ }).click()
    await expect(page).toHaveURL(/\/settings/)

    await page.screenshot({ path: 'test-results/mobile-nav-sections.png' })
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
