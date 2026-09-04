/**
 * Transcript virtualization E2E (issue #37).
 *
 * jsdom does no layout, so the windowing behavior of TranscriptView's
 * @tanstack/react-virtual list can't be asserted in unit tests (the row-
 * composition logic is unit-tested separately in transcriptRows.test.ts). These
 * Playwright tests exercise the real virtualizer against real layout via the
 * read-only ReviewView route, whose transcript is fetched over HTTP and can be
 * fully mocked — no backend or WebSocket required.
 *
 * Prerequisites:
 *   - `npm run build && npm run preview` running at http://127.0.0.1:4173
 *   - Browsers installed: `npx playwright install`
 *
 * Run: npx playwright test e2e/transcript-virtualization.spec.ts
 */

import { test, expect, type Page } from '@playwright/test'

const SESSION_ID = 'sess-long'
const EXCHANGES = 300 // user+assistant per exchange → 600 turns
const OPUS = 'claude-opus-4-8'
const HAIKU = 'claude-haiku-4-5'
// The assistant turn (0-based over assistant turns) at which the model switches
// from Opus to Haiku, producing exactly one model-switch divider near the top.
const SWITCH_AT = 2

/** Synthesize a long transcript as the wire events ReviewView folds into turns. */
function buildTranscriptEvents() {
  const events: Array<Record<string, unknown>> = []
  for (let i = 0; i < EXCHANGES; i++) {
    const userId = `u${i}`
    const asstId = `a${i}`
    events.push({ type: 'user', turnId: userId, text: `User message ${i}` })
    events.push({ type: 'turnStart', turnId: asstId })
    events.push({ type: 'turnModel', turnId: asstId, model: i < SWITCH_AT ? OPUS : HAIKU })
    events.push({ type: 'textDelta', turnId: asstId, text: `Assistant reply number ${i}` })
    events.push({ type: 'turnEnd', turnId: asstId })
  }
  return events
}

async function mockTranscript(page: Page) {
  // ReviewView only fetches the transcript; stub the rest defensively so nothing
  // hangs the app shell if a route pulls sessions/accounts. The real endpoint
  // (server/api/sessions.py get_session_transcript) always returns earliestTurn/
  // totalTurns as top-level metadata alongside events — replayComplete is a
  // WS-only wire event and is never embedded in the REST transcript payload;
  // ReviewView itself synthesizes that dispatch from the top-level fields
  // (ReviewView.tsx:38). Query-string variants (`?limit=…`) must still match.
  await page.route('**/api/sessions/*/transcript*', (route) =>
    route.fulfill({ json: { events: buildTranscriptEvents(), earliestTurn: 0, totalTurns: EXCHANGES * 2 } }),
  )
  await page.route('**/api/sessions*', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/accounts*', (route) => route.fulfill({ json: [] }))
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage(() => {}))
}

/** Distance in px from the bottom of the scroll container's scrollable range. */
async function distanceFromBottom(page: Page) {
  return page.getByTestId('transcript-scroll').evaluate(
    (el) => el.scrollHeight - el.scrollTop - el.clientHeight,
  )
}

test.describe('Transcript virtualization (#37)', () => {
  // `/review/:id` is a route at every width now — `DesktopShell`, which navigated
  // with `useState` and never matched it, is gone (Task 17). The phone viewport
  // stays because it is the environment this issue targets (transcript
  // memory/scroll perf on the mobile-accessible UI), not because it selects a
  // different component.
  test.use({ viewport: { width: 390, height: 844 } })

  test('keeps the DOM bounded and pins to the bottom on a long transcript', async ({ page }) => {
    await mockTranscript(page)
    await page.goto(`/review/${SESSION_ID}`)

    const scroller = page.getByTestId('transcript-scroll')
    await expect(scroller).toBeVisible()

    // The last exchange's reply is rendered — proof we auto-pinned to the bottom
    // rather than resting at the top.
    await expect(page.getByText(`Assistant reply number ${EXCHANGES - 1}`)).toBeVisible()

    // Only a windowed slice of the 600 turns is in the DOM, not all of them.
    const domRows = await scroller.locator('[data-index]').count()
    expect(domRows).toBeGreaterThan(0)
    expect(domRows).toBeLessThan(40)

    // Pinned to the true bottom (within a row's height).
    expect(await distanceFromBottom(page)).toBeLessThan(150)

    // A turn from the far top is virtualized away — not present in the DOM.
    await expect(page.getByText('Assistant reply number 0')).toHaveCount(0)
  })

  test('scrolling up reveals history without yanking back to the bottom', async ({ page }) => {
    await mockTranscript(page)
    await page.goto(`/review/${SESSION_ID}`)

    const scroller = page.getByTestId('transcript-scroll')
    await expect(page.getByText(`Assistant reply number ${EXCHANGES - 1}`)).toBeVisible()

    // Jump to the very top; the model-switch divider and the first reply live here.
    await scroller.evaluate((el) => { el.scrollTop = 0 })

    await expect(page.getByText('Assistant reply number 0')).toBeVisible()
    await expect(page.getByText(`Assistant reply number ${EXCHANGES - 1}`)).toHaveCount(0)

    // Exactly one model-switch divider was produced (Opus → Haiku at SWITCH_AT).
    await expect(page.getByRole('separator', { name: /Model switched/i })).toHaveCount(1)

    // The auto-pin must NOT drag the reader back down: give any stray pin a beat,
    // then confirm we're still near the top, far from the bottom.
    await page.waitForTimeout(200)
    expect(await distanceFromBottom(page)).toBeGreaterThan(1000)
  })
})
