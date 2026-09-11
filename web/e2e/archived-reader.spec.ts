/**
 * Finish-line row 6 — E2E smoke for the archived Reader page: one h1 named
 * after the session, a Back link, the transcript scroller, and NO new-session
 * dock and NO composer behind it (page-migration §2.2 as amended).
 *
 * Same harness as `delivery.spec.ts`: `npm run build && npm run preview` at
 * http://127.0.0.1:4173 (Playwright's `webServer` starts it); there is NO
 * backend — every `/api/*` call is intercepted below.
 *
 * Run: npx playwright test e2e/archived-reader.spec.ts
 */
import { test, expect, type Page } from '@playwright/test'

const SESSION = {
  id: 'arc-1', name: 'Implement issue 16', account: 'alice', cwd: '/tmp/x', repo: 'o/r', engine: 'claude',
  engineSessionId: 'c-1', permissionMode: 'default',
  createdAt: '2026-06-01T10:00:00Z', archivedAt: '2026-06-02T10:00:00Z',
}

const EVENTS = [
  { type: 'user', turnId: 'u0', text: 'Hello from the past' },
  { type: 'turnStart', turnId: 'a0' },
  { type: 'textDelta', turnId: 'a0', text: 'An archived reply' },
  { type: 'turnEnd', turnId: 'a0' },
]

async function mockApi(page: Page) {
  // Registered FIRST so the specific routes below (registered later) win —
  // Playwright matches the LAST registered route first. `[]`, not `{}`: the
  // shell's `useNavBadges` runs app-wide and reduces over `/automations` and
  // the PR list unconditionally — an unmocked object shape throws
  // "reduce is not a function" (the `dashboard.spec.ts` catch-all convention).
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions/*/transcript*', (route) =>
    route.fulfill({ json: { events: EVENTS, earliestTurn: 0, totalTurns: 2 } }))
  await page.route('**/api/sessions/archived*', (route) => route.fulfill({ json: [SESSION] }))
  await page.route('**/api/sessions*', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/accounts*', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/pm/triage*', (route) => route.fulfill({ json: [] }))
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage(() => {}))
}

for (const [name, viewport] of [['phone', { width: 390, height: 844 }], ['desktop', { width: 1440, height: 900 }]] as const) {
  test.describe(`archived reader — ${name}`, () => {
    test.use({ viewport })

    test('is a Reader: h1, Back link, transcript, no dock, no composer', async ({ page }) => {
      await mockApi(page)
      await page.goto('/sessions/archived/arc-1')
      await expect(page.getByRole('heading', { level: 1, name: 'Implement issue 16' })).toBeVisible()
      await expect(page).toHaveTitle('Implement issue 16 — ViberPit')
      await expect(page.locator('[data-page-variant="reader"]')).toHaveCount(1)
      await expect(page.getByRole('link', { name: 'Back to archived sessions' })).toBeVisible()
      await expect(page.getByTestId('transcript-scroll')).toBeVisible()
      await expect(page.getByText('An archived reply')).toBeVisible()
      await expect(page.locator('[data-region="app.dock"]')).toHaveCount(0)
      await expect(page.getByRole('textbox')).toHaveCount(0)
    })

    test('Back returns to the archived list, where the dock is back', async ({ page }) => {
      await mockApi(page)
      await page.goto('/sessions/archived/arc-1')
      await page.getByRole('link', { name: 'Back to archived sessions' }).click()
      await expect(page).toHaveURL(/\/sessions\/archived$/)
      await expect(page.locator('[data-region="app.dock"]')).toHaveCount(1)
    })
  })
}
