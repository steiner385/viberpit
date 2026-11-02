/**
 * The session rail (D-6): the cards are a right column when the session pane
 * is 64rem or wider, and a Details tab (last in the strip) below that — the
 * pane's own width, so the pushed assistant drawer folds the rail as a narrow
 * window does. No width may scroll the pane sideways. The API is mocked with
 * `page.route`, like the other session specs.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'session-rail'

async function openSession(page: Page, width: number, query = '') {
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Session rail', accountName: 'alice', engine: 'claude_code', status: 'idle',
    branch: 'feat/rail', cwd: '/home/u/worktrees/rail',
    createdAt: '2026-11-02T10:00:00Z', updatedAt: '2026-11-02T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string }
    if (frame.type === 'attach') ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
  }))
  await page.goto(`/${query}`)
  await page.getByRole('button', { name: /open session session rail/i }).click()
}

/** Neither the session pane nor the page scrolls sideways. */
async function noOverflow(page: Page) {
  const pane = page.locator('[data-page-variant="workspace"]')
  const p = await pane.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
  expect(p.scrollWidth).toBeLessThanOrEqual(p.clientWidth)
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
}

const rail = (page: Page) => page.getByRole('complementary', { name: 'Session details' })

test('1440px: the rail is a column beside the transcript, with all four cards', async ({ page }) => {
  await openSession(page, 1440)
  await expect(rail(page)).toBeVisible()
  for (const name of ['Changes', 'Context', 'Environment', 'Tasks']) {
    await expect(rail(page).getByRole('region', { name })).toBeVisible()
  }
  await expect(rail(page).getByText('feat/rail')).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Details' })).toHaveCount(0)
  await noOverflow(page)

  // Collapse it; the choice survives a reload.
  await page.getByRole('button', { name: 'Hide details' }).click()
  await expect(rail(page)).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('button', { name: 'Show details' })).toBeVisible()
  await expect(rail(page)).toHaveCount(0)
  await noOverflow(page)
})

test('1280px with the assistant drawer pushed: the rail folds into a Details tab', async ({ page }) => {
  await openSession(page, 1280, '?drawer=assistant')
  // The drawer is sticky, so it stays open after the session navigation drops `?drawer=`.
  await expect(page.getByRole('complementary', { name: 'Assistant' })).toBeVisible()
  await expect(rail(page)).toHaveCount(0)
  const details = page.getByRole('tab', { name: 'Details' })
  await expect(details).toBeVisible()
  await details.click()
  await expect(page).toHaveURL(/view=details/)
  await expect(page.locator('#tab-panel-details').getByRole('region', { name: 'Environment' })).toBeVisible()
  await noOverflow(page)
})

test('390px: a Details tab, and the strip fits', async ({ page }) => {
  await openSession(page, 390)
  await expect(rail(page)).toHaveCount(0)
  const strip = page.getByRole('tablist', { name: 'Session view' })
  await expect(strip.getByRole('tab').last()).toHaveText('Details')
  const s = await strip.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
  expect(s.scrollWidth).toBeLessThanOrEqual(s.clientWidth)
  await page.getByRole('tab', { name: 'Details' }).click()
  for (const name of ['Changes', 'Context', 'Environment', 'Tasks']) {
    await expect(page.locator('#tab-panel-details').getByRole('region', { name })).toBeAttached()
  }
  await noOverflow(page)
})
