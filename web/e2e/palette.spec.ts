/**
 * The ⌘K palette (D-10) in a real browser, at desktop and at phone width:
 * Ctrl+K reaches the page (Chrome's own Ctrl+K "search" is not reserved, so the
 * page's preventDefault wins), the lazy chunk loads, typing filters, Enter
 * lands on the session. Route-mocked: no backend.
 */
import { test, expect, type Page } from '@playwright/test'

const SESSIONS = [
  { id: 'pal-a', name: 'Refactor the rocket', repo: 'acme/rocket', branch: 'feat/thrust', updatedAt: '2026-11-02T10:00:00Z' },
  { id: 'pal-b', name: 'Polish the landing page', repo: 'acme/site', branch: 'main', updatedAt: '2026-11-01T10:00:00Z' },
]

async function mockApp(page: Page, width: number, height: number) {
  await page.setViewportSize({ width, height })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({
    json: SESSIONS.map((s) => ({
      ...s, accountName: 'alice', engine: 'claude_code', status: 'idle', createdAt: '2026-11-01T09:00:00Z', prs: [],
    })),
  }))
  await page.route('**/api/sessions/archived', (route) => route.fulfill({
    json: [{
      id: 'pal-old', name: 'Ancient voyage', account: 'alice', cwd: '/x', repo: null, engine: 'claude_code',
      engineSessionId: null, permissionMode: 'default', createdAt: '2026-10-01T10:00:00Z', archivedAt: '2026-10-02T10:00:00Z',
    }],
  }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.routeWebSocket('**/ws', () => {})
}

for (const [label, width, height] of [['desktop 1440', 1440, 900], ['phone 390', 390, 844]] as const) {
  test.describe(label, () => {
    test('Ctrl+K opens the palette; type, Enter, land on the session', async ({ page }) => {
      await mockApp(page, width, height)
      await page.goto('/sessions')
      await expect(page.getByRole('button', { name: /open session refactor the rocket/i })).toBeVisible()

      await page.keyboard.press('Control+KeyK')
      const palette = page.getByRole('dialog', { name: 'Command palette' })
      await expect(palette).toBeVisible()
      const input = palette.getByRole('combobox')
      await expect(input).toBeFocused()

      // Empty query: recent sessions first, then actions.
      await expect(palette.getByRole('option').first()).toContainText('Refactor the rocket')
      await expect(palette.getByRole('group', { name: 'Actions' })).toBeVisible()

      // The panel fits the viewport — no sideways scroll at 390.
      const box = await palette.boundingBox()
      expect(box).not.toBeNull()
      expect(box!.x).toBeGreaterThanOrEqual(0)
      expect(box!.x + box!.width).toBeLessThanOrEqual(width)

      await page.keyboard.type('landing')
      await expect(palette.getByRole('option').first()).toContainText('Polish the landing page')
      await page.keyboard.press('Enter')

      await expect(page).toHaveURL(/\/sessions\/pal-b$/)
      await expect(palette).toBeHidden()
    })

    test('Ctrl+K toggles it closed, and Esc closes it too', async ({ page }) => {
      await mockApp(page, width, height)
      await page.goto('/sessions')
      await expect(page.getByRole('button', { name: /open session refactor the rocket/i })).toBeVisible()
      const palette = page.getByRole('dialog', { name: 'Command palette' })

      await page.keyboard.press('Control+KeyK')
      await expect(palette).toBeVisible()
      await page.keyboard.press('Control+KeyK')
      await expect(palette).toBeHidden()

      await page.keyboard.press('Control+KeyK')
      await expect(palette).toBeVisible()
      await page.keyboard.press('Escape')
      await expect(palette).toBeHidden()
      await expect(page).toHaveURL(/\/sessions$/)
    })
  })
}

test('an archived session opens in the archived reader', async ({ page }) => {
  await mockApp(page, 1440, 900)
  await page.goto('/sessions')
  await expect(page.getByRole('button', { name: /open session refactor the rocket/i })).toBeVisible()
  await page.keyboard.press('Control+KeyK')
  const palette = page.getByRole('dialog', { name: 'Command palette' })
  await expect(palette.getByRole('combobox')).toBeFocused()
  await page.keyboard.type('ancient')
  await expect(palette.getByRole('option').first()).toContainText('Ancient voyage')
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/\/sessions\/archived\/pal-old$/)
})
