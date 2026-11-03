/**
 * ⌘F transcript search (D-11) in a real browser.
 *
 * A 300-turn live session opens on its tail (turns 280–299, the WS replay); the one
 * hit sits at ordinal 5, far above the loaded history. Next must page older
 * history in through the transcript's own load-older path — contiguously, from
 * `earliestTurn`, never a page cut at the hit — then land the hit centred and
 * highlighted. jsdom can show none of that layout. Route-mocked: no backend.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'
import type { SessionEvent } from '../src/session/events'

const SID = 'search-long'
const FILES_SID = 'search-files'
const TOTAL = 300
const TAIL = 20
const PAGE = 20
const HIT = 5
const NEEDLE = 'zebra'

/** Exchange n: the prompt, then a reply a few paragraphs tall, holding ordinal n. */
function exchange(n: number): SessionEvent[] {
  const reply = Array.from({ length: 4 }, (_, p) => `Reply ${n}, paragraph ${p}.`)
  if (n === HIT) reply[2] = `Reply ${n} is the one that mentions the ${NEEDLE}.`
  return [
    { type: 'user', turnId: `u${n}`, text: `Prompt number ${n}` },
    { type: 'turnStart', turnId: `a${n}`, turnIndex: n },
    { type: 'textDelta', turnId: `a${n}`, text: reply.join('\n\n') },
    { type: 'turnEnd', turnId: `a${n}` },
  ]
}

const range = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => from + i)

async function mockApp(page: Page, width: number) {
  await page.setViewportSize({ width, height: width < 600 ? 844 : 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({
    json: [
      { id: SID, name: 'Long session', accountName: 'alice', engine: 'claude_code', status: 'idle',
        createdAt: '2026-11-01T10:00:00Z', updatedAt: '2026-11-01T10:00:00Z' },
      { id: FILES_SID, name: 'Files session', accountName: 'alice', engine: 'claude_code', status: 'idle',
        cwd: '/home/u/work', createdAt: '2026-11-01T09:00:00Z', updatedAt: '2026-11-01T09:00:00Z' },
    ],
  }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route(`**/api/sessions/${FILES_SID}/files**`, (route) =>
    route.fulfill({ json: { files: [], truncated: false } }))

  // Older pages, exactly as the server cuts them: the PAGE turns before `before`.
  const befores: number[] = []
  await page.route(`**/api/sessions/${SID}/transcript?**`, (route) => {
    const url = new URL(route.request().url())
    const before = Number(url.searchParams.get('before'))
    befores.push(before)
    const earliest = Math.max(0, before - PAGE)
    route.fulfill({ json: { events: range(earliest, before).flatMap(exchange), earliestTurn: earliest, totalTurns: TOTAL } })
  })
  const searches: string[] = []
  await page.route(`**/api/sessions/${SID}/search?**`, (route) => {
    const q = new URL(route.request().url()).searchParams.get('q') ?? ''
    searches.push(q)
    const hits = q.toLowerCase().includes(NEEDLE)
      ? [{ turnId: `a${HIT}`, ordinal: HIT, role: 'assistant', snippet: `the one that mentions the ${NEEDLE}.`,
           matchStart: 26, matchEnd: 31 }]
      : []
    route.fulfill({ json: { hits, truncated: false, totalTurns: TOTAL } })
  })

  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string; sessionId?: string }
    if (frame.type !== 'attach' || !frame.sessionId) return
    const tail = frame.sessionId === SID ? range(TOTAL - TAIL, TOTAL).flatMap(exchange) : exchange(0)
    ws.send(wire(replayFrame(frame.sessionId, tail, {
      earliestTurn: frame.sessionId === SID ? TOTAL - TAIL : 0, totalTurns: frame.sessionId === SID ? TOTAL : 1,
    })))
  }))
  return { befores, searches }
}

/** The opened session has rendered — so the shell's key handlers know it is open. */
const sessionShown = (page: Page) =>
  expect(page.getByRole('button', { name: /back to session list/i })).toBeVisible()

for (const width of [1440, 390]) {
  test.describe(`at ${width}px`, () => {
    test('⌘F searches, Next pages back to the hit at ordinal 5, centres and highlights it; Esc closes', async ({ page }) => {
      const { befores, searches } = await mockApp(page, width)
      await page.goto(`/sessions/${SID}`)
      await sessionShown(page)
      await expect(page.getByText(`Reply ${TOTAL - 1}, paragraph 3.`)).toBeVisible()

      // From the transcript itself: a click leaves focus on the scroller.
      const scroller = page.getByTestId('transcript-scroll')
      await scroller.click({ position: { x: 5, y: 5 } })
      await page.keyboard.press('Control+f')
      const input = page.getByRole('searchbox', { name: 'Search this transcript' })
      await expect(input).toBeFocused()

      await input.fill(NEEDLE)
      await expect(page.getByText('0 of 1')).toBeVisible()
      expect(searches).toEqual([NEEDLE]) // debounced: one request for the whole word

      await page.keyboard.press('Enter')
      await expect(page.getByText('1 of 1')).toBeVisible()

      // Paged back contiguously from the tail, one page at a time, down to the hit.
      const current = scroller.locator('mark[data-search-current]')
      await expect(current).toBeVisible({ timeout: 15_000 })
      await expect(current).toHaveText(NEEDLE)
      const expected = range(0, (TOTAL - TAIL) / PAGE).map((i) => TOTAL - TAIL - i * PAGE).filter((b) => b > HIT)
      expect(befores).toEqual(expected)

      // Centred: the hit's row straddles the scroller's middle.
      await expect.poll(async () => {
        const box = await scroller.boundingBox()
        const row = await scroller.locator('[data-index]', { has: page.locator('mark[data-search-current]') }).boundingBox()
        if (!box || !row) return Number.POSITIVE_INFINITY
        return Math.abs((row.y + row.height / 2) - (box.y + box.height / 2))
      }).toBeLessThan(80)
      // Held there, unpinned, with the way back offered.
      await expect(page.getByRole('button', { name: 'Scroll to bottom' })).toBeVisible()

      // Esc closes the bar, drops the marks, and returns focus to the transcript.
      await page.keyboard.press('Escape')
      await expect(input).toHaveCount(0)
      await expect(scroller.locator('mark')).toHaveCount(0)
      await expect(scroller).toBeFocused()
      await expect(page).toHaveURL(new RegExp(`/sessions/${SID}$`))
    })
  })
}

test('⌘F on the Files tab is left to the browser — not prevented, no bar', async ({ page }) => {
  await mockApp(page, 1440)
  await page.goto(`/sessions/${FILES_SID}?view=files`)
  await sessionShown(page)
  await expect(page.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true')
  // Registered after the app's own listener, so it sees the final verdict.
  await page.evaluate(() => {
    window.addEventListener('keydown', (e) => {
      ;(window as unknown as { lastPrevented?: boolean }).lastPrevented = e.defaultPrevented
    })
  })
  await page.getByRole('tab', { name: 'Files' }).focus()
  await page.keyboard.press('Control+f')
  expect(await page.evaluate(() => (window as unknown as { lastPrevented?: boolean }).lastPrevented)).toBe(false)
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur())
  await page.keyboard.press('Control+f')
  expect(await page.evaluate(() => (window as unknown as { lastPrevented?: boolean }).lastPrevented)).toBe(false)
  await expect(page.getByRole('searchbox', { name: 'Search this transcript' })).toHaveCount(0)
})
