/**
 * WCAG 2.2 AA 2.5.8 (Target Size Minimum) for the Sessions list-controls
 * strip on a real coarse pointer.
 *
 * `SessionListControls` (search input, the Status/Project/Environment
 * selects, group-by/sort-by, and the "Clear filters" affordance) all share
 * `listControlClass` (`web/src/ui/list/listControlStyles.ts`), which was a
 * flat `text-xs px-2 py-1` box — well under the 44px `--target` token every
 * other primary control (`Button size="lg"`, `IconButton`, `CardMenu`'s
 * per-row trigger) already carries (docs/ACCESSIBILITY.md §4). Desktop-mouse
 * Chromium reports `hover: hover`/`pointer: fine` even at a phone-sized
 * viewport, so this only reproduces on an emulated touch/coarse pointer —
 * hence `hasTouch`/`isMobile` below, not just a narrow viewport.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const MOCK_SESSIONS = Array.from({ length: 3 }, (_, i) => ({
  id: `sess-${i}`,
  name: `Session ${i}`,
  accountName: 'alice',
  repo: 'steiner385/viberpit',
  status: 'working',
  createdAt: '2026-06-19T10:00:00Z',
  updatedAt: '2026-06-19T10:00:00Z',
}))

async function interceptApi(page: Page) {
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/sessions') route.fulfill({ json: MOCK_SESSIONS })
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

const targetBox = async (locator: ReturnType<Page['locator']>) => {
  const box = await locator.first().boundingBox()
  if (!box) throw new Error('control has no box — not rendered')
  return box
}

test.describe('Sessions list-controls touch targets — phone, coarse pointer', () => {
  test.use({ viewport: { width: 393, height: 852 }, hasTouch: true, isMobile: true })

  // On the `narrow` variant the filters and ordering fold behind one "Filters"
  // toggle (2026-09-29 critique, P1); only search and the toggle are visible
  // until it is opened, and the first session card sits within the first
  // screen instead of below ~390px of controls.
  test('folds filters behind a 44px "Filters" toggle; controls meet the target once opened', async ({ page }) => {
    await interceptApi(page)
    await page.goto('/sessions')
    await expect(page.getByTestId('session-list-controls')).toBeVisible()

    const search = await targetBox(page.getByLabel('Search sessions'))
    expect(search.height).toBeGreaterThanOrEqual(44)

    const toggle = page.getByRole('button', { name: /^Filters/ })
    expect((await targetBox(toggle)).height).toBeGreaterThanOrEqual(44)
    await expect(page.getByLabel('Sort by')).toBeHidden()
    // Folded, the strip is ONE row (search + toggle). It used to be three
    // wrapped rows of selects — the strip's own height, not an absolute y,
    // because the reconnect banner the mocked socket triggers shifts the page.
    const strip = page.getByTestId('session-list-controls')
    const folded = await targetBox(strip)
    expect(folded.height).toBeLessThan(80)

    await toggle.click()
    const opened = await targetBox(strip)
    expect(opened.height).toBeGreaterThan(folded.height + 80)
    const sortBy = await targetBox(page.getByLabel('Sort by'))
    expect(sortBy.height).toBeGreaterThanOrEqual(44)

    const status = await targetBox(page.getByLabel('Status', { exact: true }))
    expect(status.height).toBeGreaterThanOrEqual(44)
  })

  test('"Clear filters" meets the 44px target once a filter is applied', async ({ page }) => {
    await interceptApi(page)
    await page.goto('/sessions')
    await page.getByRole('button', { name: /^Filters/ }).click()
    await page.getByLabel('Status', { exact: true }).selectOption({ label: 'Working' })
    // The folded state still announces the filter.
    await expect(page.getByRole('button', { name: 'Filters, 1 active' })).toBeVisible()

    const clear = await targetBox(page.getByRole('button', { name: 'Clear filters' }))
    expect(clear.height).toBeGreaterThanOrEqual(44)
  })
})

test.describe('Sessions list-controls touch targets — desktop mouse', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('stays compact for a hover-capable pointer', async ({ page }) => {
    await interceptApi(page)
    await page.goto('/sessions')

    // The desktop list column is a ~20rem sidebar, so its strip folds exactly
    // like a phone's (the fold measures the strip, not the viewport): one row,
    // the ordering one click away.
    const strip = page.getByTestId('session-list-controls')
    const folded = await targetBox(strip)
    expect(folded.height).toBeLessThan(60)
    await page.getByRole('button', { name: /^Filters/ }).click()

    const sortBy = await targetBox(page.getByLabel('Sort by'))
    // Not a regression threshold on the exact px — just proving the
    // touch-target floor does not leak into the dense desktop chrome.
    expect(sortBy.height).toBeLessThan(36)
  })
})

/**
 * D-5: the Changes tab's per-line comment gutter. Its visible glyph is the same
 * 20px square on every pointer, so a diff row stays one 20px line; on a coarse
 * pointer an invisible `::before` widens the HIT area to 44px (rightward over
 * the line numbers) at the row's own height — taller would overlap the next
 * row and misdirect taps (docs/ACCESSIBILITY.md §4) — and the glyph is always
 * shown, since a touch screen has no hover to reveal it.
 */
const CHANGES_SID = 'touch-changes'
const CHANGES_DIFF = {
  base: '0123456789abcdef0123456789abcdef01234567', head: null, additions: 2, deletions: 1, truncated: false,
  files: [{
    path: 'src/a.ts', oldPath: null, status: 'modified', additions: 2, deletions: 1, binary: false, tooLarge: false,
    hunks: [
      { oldStart: 1, oldLines: 2, newStart: 1, newLines: 2, header: '', lines: [
        { kind: 'context', oldNo: 1, newNo: 1, text: 'keep' },
        { kind: 'del', oldNo: 2, newNo: null, text: 'old' },
        { kind: 'add', oldNo: null, newNo: 2, text: 'new' },
      ] },
      { oldStart: 20, oldLines: 1, newStart: 20, newLines: 2, header: '', lines: [
        { kind: 'context', oldNo: 20, newNo: 20, text: 'twenty' },
        { kind: 'add', oldNo: null, newNo: 21, text: 'twenty-one' },
      ] },
    ],
  }],
}

async function openChanges(page: Page) {
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: CHANGES_SID, name: 'Touch changes', accountName: 'alice', engine: 'claude_code', status: 'idle',
    createdAt: '2026-11-01T10:00:00Z', updatedAt: '2026-11-01T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route(`**/api/sessions/${CHANGES_SID}/diff**`, (route) => route.fulfill({ json: CHANGES_DIFF }))
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    if (JSON.parse(msg.toString()).type === 'attach') ws.send(wire(replayFrame(CHANGES_SID, [], { totalTurns: 0 })))
  }))
  await page.goto(`/sessions/${CHANGES_SID}?view=changes`)
  await expect(page.getByTestId('changes-tab')).toBeVisible()
}

/** The page never scrolls sideways. */
async function expectNoPageOverflow(page: Page) {
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
}

test.describe('Changes tab comment gutter — phone, coarse pointer', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('rows stay one line tall; each gutter has a 44px-wide, row-high hit area that never reaches a neighbour', async ({ page }) => {
    await openChanges(page)
    const buttons = page.getByRole('button', { name: /^Comment on (removed )?line \d+$/ })
    await expect(buttons.first()).toBeVisible()
    expect(await buttons.count()).toBeGreaterThanOrEqual(5)

    // (c) Row height unchanged by the touch target: one 20px line (≤ 24px).
    for (const row of await page.locator('[data-diff-row]').all()) {
      expect((await targetBox(row)).height).toBeLessThanOrEqual(24)
    }
    // Glyphs stay the dense 20px square, and are shown without hover.
    for (const b of await buttons.all()) {
      const box = await targetBox(b)
      expect(box.height).toBeLessThanOrEqual(24)
      expect(box.width).toBeLessThanOrEqual(24)
      expect(await b.evaluate((el) => getComputedStyle(el).opacity)).toBe('1')
    }

    const hitAt = (x: number, y: number) => page.evaluate(([px, py]) =>
      document.elementFromPoint(px, py)?.closest('button')?.getAttribute('aria-label') ?? null, [x, y])

    // (a) A middle row of a dense stack (`removed line 2`, between `line 1` and
    // `line 2`) and the first row: taps across the row's line-number column —
    // its top, middle and bottom third — land on THAT row's button, never a
    // neighbour's. The first row's area is not clipped by the frame either.
    for (const label of ['Comment on removed line 2', 'Comment on line 1']) {
      const g = await targetBox(page.getByRole('button', { name: label, exact: true }))
      for (const x of [g.x + g.width + 4, g.x + g.width + 12, g.x + g.width + 20]) {
        for (const y of [g.y + 2, g.y + g.height / 2, g.y + g.height - 2]) {
          expect(await hitAt(x, y), `${label} at (${x}, ${y})`).toBe(label)
        }
      }
      // (b) The hit area is at least 44px wide: its far edge still hits.
      expect(await hitAt(g.x + 43, g.y + g.height / 2)).toBe(label)
    }

    await expectNoPageOverflow(page)
  })

  test('a saved comment card fits its column on a 390px phone', async ({ page }) => {
    await openChanges(page)
    await page.getByRole('button', { name: 'Comment on line 2', exact: true }).click()
    const body = 'This rename leaves the old call sites behind, and the long unbroken identifier '
      + 'someVeryLongIdentifierNameThatWouldNotWrapOnItsOwnAtAll should wrap too.'
    await page.getByRole('textbox', { name: 'Comment' }).fill(body)
    await page.getByRole('button', { name: 'Save' }).click()
    const card = page.getByRole('article', { name: 'Comment on line 2' })
    await expect(card).toContainText('should wrap too.')

    const c = await targetBox(card)
    const frame = await targetBox(card.locator('xpath=ancestor::div[contains(@class, "hljs")][1]'))
    expect(c.x).toBeGreaterThanOrEqual(frame.x)
    expect(c.x + c.width).toBeLessThanOrEqual(frame.x + frame.width + 0.5)
    // The Edit/Delete buttons are inside the card, so in view too.
    const del = await targetBox(card.getByRole('button', { name: 'Delete' }))
    expect(del.x + del.width).toBeLessThanOrEqual(frame.x + frame.width + 0.5)
    // And the unified rows did not widen to the comment's unwrapped text.
    const scroll = await card.locator('xpath=ancestor::div[contains(@class, "hljs")][1]')
      .evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
    expect(scroll.scrollWidth).toBeLessThanOrEqual(scroll.clientWidth)

    await expectNoPageOverflow(page)
  })
})

test.describe('Changes tab comment gutter — desktop mouse', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('stays a dense 20px gutter for a hover-capable pointer', async ({ page }) => {
    await openChanges(page)
    const box = await targetBox(page.getByRole('button', { name: 'Comment on line 2', exact: true }))
    expect(box.height).toBeLessThan(24)
    expect(box.width).toBeLessThan(24)
  })
})
