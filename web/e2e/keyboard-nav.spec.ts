/**
 * D-10 navigation keys in a real browser: Alt+[ / Alt+] and Alt+Shift+1–9
 * switch sessions in the list order, Alt+↑ / Alt+↓ jump the transcript between
 * the user's own prompts — ⌘⌥ on a Mac.
 *
 * jsdom dispatches any chord and lays nothing out, so it can show neither that
 * Chromium delivers Alt+] / Alt+↑ to the page nor that the virtualized
 * transcript actually lands a prompt at the top of the viewport — this can.
 * Route-mocked: no backend.
 */
import { test, expect, type Page } from '@playwright/test'
import { assistantTurn, replayFrame, userTurn, wire } from './fixtures/wsFrames'
import type { SessionEvent } from '../src/session/events'

const IDS = ['nav-a', 'nav-b']
const EXCHANGES = 30

/** Each prompt is followed by a reply taller than the viewport, so at the bottom
 *  the topmost visible row is the last REPLY and every prompt sits above it —
 *  only a jump can bring one to the top. */
function transcript(): SessionEvent[] {
  const events: SessionEvent[] = []
  for (let i = 0; i < EXCHANGES; i++) {
    events.push(...userTurn(`u${i}`, `Prompt number ${i}`))
    const paragraphs = Array.from({ length: 40 }, (_, p) => `Reply ${i}, paragraph ${p}.`).join('\n\n')
    events.push(...assistantTurn(`a${i}`, paragraphs))
  }
  return events
}

async function mockApp(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({
    json: IDS.map((id) => ({
      id, name: `Session ${id}`, accountName: 'alice', engine: 'claude_code', status: 'idle',
      createdAt: '2026-11-01T10:00:00Z', updatedAt: '2026-11-01T10:00:00Z',
    })),
  }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  const events = transcript()
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string; sessionId?: string }
    if (frame.type === 'attach' && frame.sessionId) {
      ws.send(wire(replayFrame(frame.sessionId, events, { earliestTurn: 0, totalTurns: EXCHANGES * 2 })))
    }
  }))
}

const openId = (page: Page) => new URL(page.url()).pathname.split('/').pop() ?? ''

/** The opened session has rendered — so the shell's key handlers know it is open. */
const sessionShown = (page: Page) =>
  expect(page.getByRole('button', { name: /back to session list/i })).toBeVisible()

test('Alt+] walks the list order from no session open, and wraps; Alt+[ steps back', async ({ page }) => {
  await mockApp(page)
  await page.goto('/sessions')
  await expect(page.getByRole('button', { name: /open session session nav-a/i })).toBeVisible()

  await page.keyboard.press('Alt+BracketRight')
  await expect(page).toHaveURL(/\/sessions\/nav-[ab]$/)
  const first = openId(page)
  await sessionShown(page)

  await page.keyboard.press('Alt+BracketRight')
  await expect(page).not.toHaveURL(new RegExp(`/${first}$`))
  const second = openId(page)
  expect(IDS).toContain(second)
  await sessionShown(page)

  // Wraps past the end back to the first row.
  await page.keyboard.press('Alt+BracketRight')
  await expect(page).toHaveURL(new RegExp(`/sessions/${first}$`))
  await sessionShown(page)

  // …and Alt+[ wraps the other way.
  await page.keyboard.press('Alt+BracketLeft')
  await expect(page).toHaveURL(new RegExp(`/sessions/${second}$`))
})

test('Alt+Shift+2 opens the second row; bare Alt+2 stays the browser\'s (Linux tab select)', async ({ page }) => {
  await mockApp(page)
  await page.goto('/sessions')
  const rows = page.getByRole('button', { name: /^open session session nav-/i })
  await expect(rows).toHaveCount(2)
  const secondId = ((await rows.nth(1).getAttribute('aria-label')) ?? '').replace(/^open session session /i, '')

  await page.keyboard.press('Alt+Digit2')
  await page.waitForTimeout(200)
  await expect(page).toHaveURL(/\/sessions$/)

  await page.keyboard.press('Alt+Shift+Digit2')
  await expect(page).toHaveURL(new RegExp(`/sessions/${secondId}$`))
})

test('on a Mac the chords are ⌘⌥ — a bare ⌥] is typing and switches nothing', async ({ page }) => {
  // The platform the shell reads (`isApplePlatform`), set before the app boots.
  await page.addInitScript(() => {
    Object.defineProperty(Navigator.prototype, 'platform', { get: () => 'MacIntel' })
  })
  await mockApp(page)
  await page.goto('/sessions')
  await expect(page.getByRole('button', { name: /open session session nav-a/i })).toBeVisible()

  await page.keyboard.press('Alt+BracketRight')
  await page.waitForTimeout(200)
  await expect(page).toHaveURL(/\/sessions$/)

  await page.keyboard.press('Meta+Alt+BracketRight')
  await expect(page).toHaveURL(/\/sessions\/nav-[ab]$/)
  await sessionShown(page)
  await expect(page.getByText(`Reply ${EXCHANGES - 1}, paragraph 39.`)).toBeVisible()

  const composer = page.getByRole('textbox', { name: /^message$/i })
  await composer.focus()
  await page.keyboard.press('Meta+Alt+ArrowUp')
  await expect(page.getByRole('button', { name: 'Scroll to bottom' })).toBeVisible()
  await expect(composer).toBeFocused()
})

test('Alt+↑ / Alt+↓ jump between own prompts from inside the composer', async ({ page }) => {
  await mockApp(page)
  await page.goto(`/sessions/${IDS[0]}`)
  const scroller = page.getByTestId('transcript-scroll')
  await expect(page.getByText(`Reply ${EXCHANGES - 1}, paragraph 39.`)).toBeVisible()
  // Let the replayed rows finish measuring (markdown and fonts lay out after the
  // first paint) so the topmost visible row is the settled one.
  let lastHeight = -1
  await expect.poll(async () => {
    const h = await scroller.evaluate((el) => el.scrollHeight)
    const stable = h === lastHeight
    lastHeight = h
    return stable
  }, { intervals: [250] }).toBe(true)

  const composer = page.getByRole('textbox', { name: /^message$/i })
  await composer.focus()

  /** How far the prompt's ROW sits from the scroller's top edge — ~0 when it was jumped to. */
  const scrollerTop = async () => (await scroller.boundingBox())?.y ?? 0
  const promptOffset = async (n: number) => {
    const row = scroller.locator('[data-index]', { has: page.getByText(`Prompt number ${n}`, { exact: true }) })
    const box = await row.boundingBox()
    return box === null ? Number.POSITIVE_INFINITY : Math.abs(box.y - (await scrollerTop()))
  }

  await page.keyboard.press('Alt+ArrowUp')
  await expect(page.getByText(`Prompt number ${EXCHANGES - 1}`, { exact: true })).toBeVisible()
  await expect.poll(() => promptOffset(EXCHANGES - 1)).toBeLessThan(2)
  // Unpinned: the view stays where it jumped, with the way back offered.
  await expect(page.getByRole('button', { name: 'Scroll to bottom' })).toBeVisible()

  await page.keyboard.press('Alt+ArrowUp')
  await expect.poll(() => promptOffset(EXCHANGES - 2)).toBeLessThan(2)

  await page.keyboard.press('Alt+ArrowDown')
  await expect.poll(() => promptOffset(EXCHANGES - 1)).toBeLessThan(2)

  // Focus never left the composer.
  await expect(composer).toBeFocused()
})
