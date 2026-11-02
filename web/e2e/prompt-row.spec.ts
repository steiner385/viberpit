/**
 * The Desktop-style prompt row (D-1): the keyboard menus reach the page in a real
 * browser, and the row fits at phone, tablet and desktop widths.
 *
 * jsdom can dispatch any chord, so it cannot show that Chromium delivers
 * Alt+Shift+M to the page rather than keeping it — this can.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'prompt-row'

async function openSession(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Prompt row', accountName: 'alice', engine: 'claude_code', status: 'idle',
    createdAt: '2026-11-01T10:00:00Z', updatedAt: '2026-11-01T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    if (JSON.parse(msg.toString()).type === 'attach') ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session prompt row/i }).click()
  await expect(page.getByTestId('prompt-row')).toBeVisible()
}

test('Alt+Shift+M opens the mode menu from the composer, Alt+Shift+I the model menu', async ({ page }) => {
  await openSession(page, 1440)
  const composer = page.getByRole('textbox', { name: /^message$/i })
  await composer.focus()
  await page.keyboard.press('Alt+Shift+KeyM')
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  await expect(menu.getByText('Ask')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(menu).toBeHidden()
  await composer.focus()
  await page.keyboard.press('Alt+Shift+KeyI')
  await expect(page.getByRole('menu')).toBeVisible()
})

for (const width of [390, 820, 1440]) {
  test(`the prompt row fits at ${width}px`, async ({ page }) => {
    await openSession(page, width)
    const fits = await page.getByTestId('prompt-row').evaluate((el) => ({
      scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
    }))
    expect(fits.scrollWidth).toBeLessThanOrEqual(fits.clientWidth)
  })
}
