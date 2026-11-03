/**
 * ⌘F transcript search (D-11) in a real browser.
 *
 * A 300-turn live session opens on its tail (turns 280–299, the WS replay). Two
 * hits: the newest at ordinal 295 (loaded), the other at ordinal 5, far above the
 * loaded history, in a reply taller than the viewport with the match near its top.
 *
 *  - Results land on the NEWEST hit (it is loaded — no paging while typing).
 *  - Enter goes OLDER: it pages older history in through the transcript's own
 *    load-older path — contiguously, from `earliestTurn`, never a page cut at the
 *    hit — then centres the MATCH itself, not the tall turn's middle.
 *  - Scroll to bottom mid-paging abandons the jump: no more pages, no yank.
 *
 * jsdom can show none of that layout. Route-mocked: no backend.
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
const NEWEST = 295
const NEEDLE = 'zebra'
const TALL = 40

const tallLine = `Reply ${HIT} is the one that mentions the ${NEEDLE}.`
const newestLine = `Reply ${NEWEST} also names a ${NEEDLE}.`

/** Exchange n: the prompt, then a reply holding ordinal n — a few paragraphs, or
 *  (the hit at HIT) far taller than the viewport, its match in the second paragraph. */
function exchange(n: number): SessionEvent[] {
  const reply = Array.from({ length: n === HIT ? TALL : 4 }, (_, p) => `Reply ${n}, paragraph ${p}.`)
  if (n === HIT) reply[1] = tallLine
  if (n === NEWEST) reply[2] = newestLine
  return [
    { type: 'user', turnId: `u${n}`, text: `Prompt number ${n}` },
    { type: 'turnStart', turnId: `a${n}`, turnIndex: n },
    { type: 'textDelta', turnId: `a${n}`, text: reply.join('\n\n') },
    { type: 'turnEnd', turnId: `a${n}` },
  ]
}

const range = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => from + i)
const snippetHit = (n: number, line: string) =>
  ({ turnId: `a${n}`, ordinal: n, role: 'assistant', snippet: line,
     matchStart: line.indexOf(NEEDLE), matchEnd: line.indexOf(NEEDLE) + NEEDLE.length })

async function mockApp(page: Page, width: number, { pageDelayMs = 0 } = {}) {
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
  await page.route(`**/api/sessions/${SID}/transcript?**`, async (route) => {
    const url = new URL(route.request().url())
    const before = Number(url.searchParams.get('before'))
    befores.push(before)
    if (pageDelayMs) await new Promise((r) => setTimeout(r, pageDelayMs))
    const earliest = Math.max(0, before - PAGE)
    await route.fulfill({ json: { events: range(earliest, before).flatMap(exchange), earliestTurn: earliest, totalTurns: TOTAL } })
  })
  const searches: string[] = []
  await page.route(`**/api/sessions/${SID}/search?**`, (route) => {
    const q = new URL(route.request().url()).searchParams.get('q') ?? ''
    searches.push(q)
    const hits = q.toLowerCase().includes(NEEDLE) ? [snippetHit(HIT, tallLine), snippetHit(NEWEST, newestLine)] : []
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

/** Open the long session, ⌘F from the clicked transcript, and search the needle. */
async function openAndSearch(page: Page) {
  await page.goto(`/sessions/${SID}`)
  await sessionShown(page)
  await expect(page.getByText(`Reply ${TOTAL - 1}, paragraph 3.`)).toBeVisible()
  const scroller = page.getByTestId('transcript-scroll')
  await scroller.click({ position: { x: 5, y: 5 } })
  await page.keyboard.press('Control+f')
  const input = page.getByRole('searchbox', { name: 'Search this transcript' })
  await expect(input).toBeFocused()
  await input.fill(NEEDLE)
  return { scroller, input }
}

/** How far the current mark's centre sits from the scroller's. */
async function markOffCentre(page: Page) {
  const box = await page.getByTestId('transcript-scroll').boundingBox()
  const mark = await page.locator('mark[data-search-current]').boundingBox()
  if (!box || !mark) return Number.POSITIVE_INFINITY
  return Math.abs((mark.y + mark.height / 2) - (box.y + box.height / 2))
}

for (const width of [1440, 390]) {
  test.describe(`at ${width}px`, () => {
    test('results land on the newest hit; Enter pages back to the older one and centres its match; Esc closes', async ({ page }) => {
      const { befores, searches } = await mockApp(page, width)
      const { scroller, input } = await openAndSearch(page)

      // The newest hit is loaded: the answer lands on it, without paging.
      await expect(page.getByText('2 of 2')).toBeVisible()
      expect(searches).toEqual([NEEDLE]) // debounced: one request for the whole word
      // Page-rooted: a scroller-rooted locator inside `has:` would look for a scroller in the row.
      const current = page.locator('mark[data-search-current]')
      await expect(current).toBeVisible()
      await expect(scroller.locator('[data-index]', { has: current })).toContainText(newestLine)
      expect(befores).toEqual([])

      // Enter goes older: paged back contiguously from the tail, one page at a time.
      await page.keyboard.press('Enter')
      await expect(page.getByText('1 of 2')).toBeVisible()
      await expect(scroller.locator('[data-index]', { has: current })).toContainText(tallLine, { timeout: 15_000 })
      await expect(current).toHaveText(NEEDLE)
      const expected = range(0, (TOTAL - TAIL) / PAGE).map((i) => TOTAL - TAIL - i * PAGE).filter((b) => b > HIT)
      expect(befores).toEqual(expected)

      // The reply is far taller than the viewport and its match near its top: the
      // MATCH is centred, not the reply's middle.
      const row = await scroller.locator('[data-index]', { has: current }).boundingBox()
      const box = await scroller.boundingBox()
      expect(row!.height).toBeGreaterThan(box!.height)
      await expect.poll(() => markOffCentre(page)).toBeLessThan(40)
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

test('Scroll to bottom mid-paging abandons the jump — no more pages, no yank, no stale current hit', async ({ page }) => {
  const { befores } = await mockApp(page, 1440, { pageDelayMs: 400 })
  const { scroller } = await openAndSearch(page)
  await expect(page.getByText('2 of 2')).toBeVisible()
  await page.keyboard.press('Enter') // older: starts paging toward ordinal 5
  await expect.poll(() => befores.length).toBeGreaterThan(0)
  await page.getByRole('button', { name: 'Scroll to bottom' }).click()
  // The page in flight still lands; no further page is asked for.
  await page.waitForTimeout(1500)
  expect(befores.length).toBeLessThanOrEqual(2)
  // Back at the bottom, and staying there; the old current hit was cleared.
  await expect.poll(() => scroller.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(2)
  await expect(page.locator('mark[data-search-current]')).toHaveCount(0)
  await expect(page.getByText(`Reply ${TOTAL - 1}, paragraph 3.`).first()).toBeVisible()
})

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
