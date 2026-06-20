/**
 * T044 — Dashboard E2E tests.
 *
 * Prerequisites:
 *   - `npm run build && npm run preview` (or `npm run dev`) running at http://127.0.0.1:4173
 *   - Browsers installed: `npx playwright install`
 *   - The backend API is intercepted via Playwright's route interception so no real server is needed.
 *
 * Run: npx playwright test e2e/dashboard.spec.ts
 */

import { test, expect, type Page } from '@playwright/test'

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

async function interceptApi(page: Page) {
  await page.route('/api/sessions', (route) => {
    route.fulfill({ json: MOCK_SESSIONS })
  })
  await page.route('/api/accounts', (route) => {
    route.fulfill({ json: MOCK_ACCOUNTS })
  })
  // Intercept WebSocket so it doesn't try to connect
  await page.routeWebSocket('/ws', (ws) => {
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
    await page.goto('/')

    // Heading visible
    await expect(page.getByRole('heading', { name: /ccswitch/i })).toBeVisible()

    // Session list loads
    await expect(page.getByText('Work session')).toBeVisible()
    await expect(page.getByText('alice')).toBeVisible()
  })

  test('clicking Open on a session opens the Terminal view', async ({ page }) => {
    await page.goto('/')

    await expect(page.getByText('Work session')).toBeVisible()

    // Click the session open button
    await page.getByRole('button', { name: /open session work session/i }).click()

    // Terminal view should be shown — dashboard heading gone
    await expect(page.getByRole('heading', { name: /ccswitch/i })).not.toBeVisible()
    // Back button is present
    await expect(page.getByRole('button', { name: /back to session list/i })).toBeVisible()
  })

  test('clicking Back returns to the dashboard', async ({ page }) => {
    await page.goto('/')

    await expect(page.getByText('Work session')).toBeVisible()
    await page.getByRole('button', { name: /open session work session/i }).click()

    // Click back
    await page.getByRole('button', { name: /back to session list/i }).click()

    // Dashboard heading visible again
    await expect(page.getByRole('heading', { name: /ccswitch/i })).toBeVisible()
  })

  test('mobile viewport: no horizontal overflow, helper key bar visible', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await page.goto('/')

    // No horizontal overflow
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth)
    const viewportWidth = await page.evaluate(() => window.innerWidth)
    expect(bodyWidth).toBeLessThanOrEqual(viewportWidth)

    // Navigate to terminal view to see key bar
    await expect(page.getByText('Work session')).toBeVisible()
    await page.getByRole('button', { name: /open session work session/i }).click()

    // Helper key bar should be visible
    await expect(page.getByRole('toolbar', { name: /helper keys/i })).toBeVisible()
  })

  test('helper key bar buttons are ≥44px tall', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await page.goto('/')

    await expect(page.getByText('Work session')).toBeVisible()
    await page.getByRole('button', { name: /open session work session/i }).click()

    // Check that Escape button meets the 44px touch target requirement
    const escBtn = page.getByRole('button', { name: /escape/i })
    await expect(escBtn).toBeVisible()

    const height = await escBtn.evaluate((el) => el.getBoundingClientRect().height)
    expect(height).toBeGreaterThanOrEqual(44)
  })
})
