/**
 * The keyboard-open squeeze on the Sessions page.
 *
 * `interactive-widget=resizes-content` (index.html) makes the Android keyboard
 * shrink the LAYOUT viewport, so a phone reflows the whole shell into ~480px.
 * Every band of `SessionListColumn` except the scroll band is `shrink-0`, so
 * their combined height used to exceed the box the shell gave the column: the
 * band collapsed to ~8px, the surplus overflowed the column, and `app.dock` —
 * opaque, and a later flex item, so it paints on top — covered it. What reached
 * the screen was a stray sliver of whichever control straddled the dock's top
 * border, with every session unreachable behind it.
 *
 * These are geometry assertions on purpose. The unit tests pin the two classes
 * that carry the fix (`overflow-hidden` on the column, `short:hidden` on the
 * controls strip), but jsdom does no layout and evaluates no media query, so
 * only a real browser can show that the band actually gets its room back. A
 * class contract would still pass if the `short` variant stopped compiling.
 */
import { test, expect, type Page } from '@playwright/test'

const MOCK_SESSIONS = Array.from({ length: 13 }, (_, i) => ({
  id: `sess-${i}`,
  name: `Session ${i}`,
  accountName: 'alice',
  repo: 'steiner385/viberpit',
  status: 'working',
  createdAt: '2026-06-19T10:00:00Z',
  updatedAt: '2026-06-19T10:00:00Z',
}))

// The audit's own load: 18 active rows plus 6 queued (issue #1399's acceptance
// criteria names both counts explicitly, so the matrix below uses them rather
// than the lighter 13-session fixture above — a short viewport with only a
// handful of rows understates how much the scroll band actually has to hold).
const MOCK_SESSIONS_FULL = [
  ...Array.from({ length: 18 }, (_, i) => ({
    id: `sess-${i}`,
    name: `Session ${i}`,
    accountName: 'alice',
    repo: 'steiner385/viberpit',
    status: 'working',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  })),
  ...Array.from({ length: 6 }, (_, i) => ({
    id: `queued-${i}`,
    name: `Queued ${i}`,
    accountName: 'alice',
    repo: 'steiner385/viberpit',
    status: 'queued',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  })),
]

async function interceptApi(page: Page, sessions: unknown[] = MOCK_SESSIONS) {
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/sessions') route.fulfill({ json: sessions })
    else if (path === '/api/accounts') {
      route.fulfill({ json: [{
        name: 'alice',
        status: 'ok',
        usage: { provider: 'anthropic', fetchedAt: null, availability: 'available', windows: [] },
      }] })
    } else if (path === '/api/config') route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } })
    else if (path === '/api/daemon/health') route.fulfill({ json: { status: 'ok' } })
    else if (path === '/api/github/repos') {
      route.fulfill({ json: [{ nameWithOwner: 'steiner385/viberpit', defaultBranch: 'master' }] })
    } else route.fulfill({ json: [] })
  })
  await page.routeWebSocket('**/ws', (ws) => { ws.onMessage(() => {}) })
}

/**
 * How far the column's FIXED CHROME runs past the box the shell gave it.
 *
 * The bands, not their contents: rows inside the scroll band legitimately
 * extend below it (that is what scrolling is), and `overflow-y-auto` already
 * clips them, so descending into it would measure the session list rather than
 * the layout. What broke was the chrome — scope segment, controls strip, daemon
 * line, New Session — adding up to more than the column's height.
 *
 * Note this is a LAYOUT measure, deliberately taken despite the clip:
 * `getBoundingClientRect` ignores `overflow: hidden`, so a positive number here
 * still means "does not fit" even now that it can no longer be painted. That is
 * the point — the clip stops the bleed, but the chrome fitting is the fix.
 */
async function chromeOverflowPx(page: Page): Promise<number> {
  return page.evaluate(() => {
    const list = document.querySelector('[data-region="app.list"]')
    if (!list) throw new Error('app.list is not mounted')
    const limit = list.getBoundingClientRect().bottom
    const scroll = list.querySelector('[data-testid="session-list-scroll"]')
    let lowest = limit
    const visit = (el: Element) => {
      const r = el.getBoundingClientRect()
      // Zero-size nodes (the collapsed strip, sr-only headings) report a bottom
      // at their parent's top edge and would skew the max the wrong way.
      if (r.height > 0 && r.bottom > lowest) lowest = r.bottom
      // The band's own box counts; its scrolled contents do not.
      if (el === scroll) return
      for (const child of el.children) visit(child)
    }
    for (const child of list.children) visit(child)
    return Math.round(lowest - limit)
  })
}

const bandHeight = (page: Page) =>
  page.locator('[data-testid="session-list-scroll"]').evaluate((el) =>
    Math.round(el.getBoundingClientRect().height))

test.describe('Sessions list at a keyboard-open height', () => {
  // 393×480: a phone with the software keyboard up, the case from the report.
  test.use({ viewport: { width: 393, height: 480 } })

  test('nothing in the list paints past the dock, and the list stays usable', async ({ page }) => {
    await interceptApi(page)
    await page.goto('/sessions')
    await expect(page.getByRole('navigation', { name: /sections/i })).toBeVisible()
    await expect(page.getByRole('link', { name: /^Active/ })).toBeVisible()

    // The room: the column's fixed chrome fits inside the column again, so
    // there is no surplus for the dock to paint over. This was ~125px before.
    expect(await chromeOverflowPx(page)).toBeLessThanOrEqual(0)

    // The clip, browser-verified rather than pinned on a class: even if some
    // future band overflows again, it cannot bleed under the dock.
    const overflow = await page.locator('[data-region="app.list"] > div > div').first()
      .evaluate((el) => getComputedStyle(el).overflow)
    expect(overflow).toBe('hidden')

    // The room: the filter strip is gone, so the scroll band is a list again
    // rather than the ~8px slot it collapsed to.
    await expect(page.getByTestId('session-list-controls')).toBeHidden()
    // 8px before the fix, ~69px after (one row, and scrollable through the
    // rest). The floor is well under that: what this has to catch is the band
    // collapsing to nothing, and a threshold just below the measured value
    // would turn a font-metric difference between platforms into a red build.
    expect(await bandHeight(page)).toBeGreaterThan(40)

    // And the point of all of it — sessions you can actually see and open.
    await expect(page.getByText('Session 0')).toBeVisible()

    // The dock is still the thing anchored to the bottom edge: the prompt is
    // what the keyboard is open FOR, so it must not have been pushed off.
    await expect(page.getByPlaceholder(/New session/)).toBeVisible()
  })
})

test.describe('Sessions list at a normal phone height', () => {
  test.use({ viewport: { width: 393, height: 852 } })

  test('keeps the filter strip — the hiding is the exception, not the default', async ({ page }) => {
    await interceptApi(page)
    await page.goto('/sessions')
    await expect(page.getByRole('link', { name: /^Active/ })).toBeVisible()

    await expect(page.getByTestId('session-list-controls')).toBeVisible()
    await expect(page.getByLabel('Sort by')).toBeVisible()
    // With the strip on screen the chrome must still fit — this is the case
    // that would catch a threshold set so low the squeeze is never relieved.
    expect(await chromeOverflowPx(page)).toBeLessThanOrEqual(0)
  })
})

/**
 * Issue #1399's full acceptance matrix: the audit measured an ~8px scroll
 * band — every session unreachable behind the dock — at 320×568, 568×320 and
 * 852×393, with the New Session button covered at 568×320. 18 active rows and
 * 6 queued (the audit's own load, heavier than the 13-row fixture above) at
 * every size the finding lists: both orientations of a small phone, a tablet
 * in both orientations, the keyboard-open case already covered above, and two
 * deliberately extreme landscape heights past any real device the audit
 * measured, added because the finding's own acceptance list names them.
 *
 * Two different outcomes, not one blanket assertion:
 *
 * - 320×568 was a pure threshold miss (568px sits 8px above the pre-#1399
 *   `short:` cutoff of 560, so the controls strip never shed) and 852×393 was
 *   half fixed by the cutoff and half by `DispatchBar`'s advanced fields no
 *   longer holding their grid row open at a short height (`DispatchBar.tsx`,
 *   also #1399) — both now recover a genuinely usable band.
 * - 568×320, 852×240 and 320×256 do not, because what is left after the top
 *   bar/rail, the banner strip and the prompt bar (none of which shrink below
 *   their own content — the prompt staying full-size is deliberate, see
 *   `PageHost.tsx`'s `pageHidden` doc) is a handful of pixels no amount of
 *   the LIST's own chrome-shedding can grow. That is the "deliberate alternate
 *   layout below [96px]" the finding itself allows for: the list degrades to
 *   near-nothing rather than to a bleed. What every one of the five must not
 *   regress on is the ORIGINAL bug — chrome painting outside its own box and
 *   surfacing under the dock — and that the prompt itself, the one thing a
 *   short viewport is usually short FOR, stays fully on screen.
 */
for (const [width, height] of [[320, 568], [852, 393]] as const) {
  test.describe(`Sessions list at ${width}x${height} — recovers a real band`, () => {
    test.use({ viewport: { width, height } })

    test('the band is usable, not a sliver', async ({ page }) => {
      await interceptApi(page, MOCK_SESSIONS_FULL)
      await page.goto('/sessions')
      await expect(page.getByRole('link', { name: /^Active/ })).toBeVisible()

      // The chrome fits its own box — the bug this whole file exists to catch.
      expect(await chromeOverflowPx(page)).toBeLessThanOrEqual(0)

      // Both heights are under the `short:` cutoff (580px, raised from 560 by
      // this issue), so the filter strip sheds and the room goes to the band.
      await expect(page.getByTestId('session-list-controls')).toBeHidden()

      // The band is not a sliver: comfortably over the finding's own 96px
      // floor, with real rows in it. 217px/136px measured; the margin below
      // that catches a regression without pinning font-metric noise.
      expect(await bandHeight(page)).toBeGreaterThan(96)
      await expect(page.getByText('Session 0')).toBeVisible()

      // Navigation and the prompt are the other two surfaces the finding
      // names — both stay reachable, not just the list.
      await expect(page.getByRole('navigation', { name: /sections/i })).toBeVisible()
      await expect(page.getByPlaceholder(/New session/)).toBeVisible()
    })
  })
}

for (const [width, height] of [[568, 320], [852, 240], [320, 256]] as const) {
  test.describe(`Sessions list at ${width}x${height} — the hard floor`, () => {
    test.use({ viewport: { width, height } })

    test('no bleed, and the prompt stays reachable', async ({ page }) => {
      await interceptApi(page, MOCK_SESSIONS_FULL)
      await page.goto('/sessions')

      // The one thing that must never come back: chrome painting past its own
      // box. `chromeOverflowPx` can be positive here (some of the column's own
      // head no longer fits at all) — the clip is what makes that safe rather
      // than a repeat of the original bleed, so both are asserted.
      const overflow = await page.locator('[data-region="app.list"] > div > div').first()
        .evaluate((el) => getComputedStyle(el).overflow)
      expect(overflow).toBe('hidden')

      // The prompt bar is the surface a short viewport is usually short FOR
      // (the keyboard is up, or the window is a sliver) — it keeps its full
      // natural height rather than shrinking (`PageHost.tsx`), so it must
      // still be entirely on screen rather than pushed off past the bottom
      // edge or clipped by an ancestor.
      const textarea = page.getByPlaceholder(/New session/)
      await expect(textarea).toBeVisible()
      const box = await textarea.boundingBox()
      expect(box).not.toBeNull()
      expect(box!.y + box!.height).toBeLessThanOrEqual(height)
    })
  })
}
