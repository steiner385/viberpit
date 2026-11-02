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
 * D-5: the Changes tab's per-line comment gutter is a 20px button for a mouse
 * but must floor at 44px on a coarse pointer (the `CardMenu` `actionBtn`
 * pattern) — and be visible there, since a touch screen has no hover to reveal it.
 */
const CHANGES_SID = 'touch-changes'
const CHANGES_DIFF = {
  base: '0123456789abcdef0123456789abcdef01234567', head: null, additions: 1, deletions: 1, truncated: false,
  files: [{
    path: 'src/a.ts', oldPath: null, status: 'modified', additions: 1, deletions: 1, binary: false, tooLarge: false,
    hunks: [{ oldStart: 1, oldLines: 2, newStart: 1, newLines: 2, header: '', lines: [
      { kind: 'context', oldNo: 1, newNo: 1, text: 'keep' },
      { kind: 'del', oldNo: 2, newNo: null, text: 'old' },
      { kind: 'add', oldNo: null, newNo: 2, text: 'new' },
    ] }],
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

test.describe('Changes tab comment gutter — phone, coarse pointer', () => {
  test.use({ viewport: { width: 393, height: 852 }, hasTouch: true, isMobile: true })

  test('every gutter button is a visible 44px target', async ({ page }) => {
    await openChanges(page)
    const buttons = page.getByRole('button', { name: /^Comment on (removed )?line \d+$/ })
    await expect(buttons.first()).toBeVisible()
    const n = await buttons.count()
    expect(n).toBeGreaterThanOrEqual(3)
    for (let i = 0; i < n; i++) {
      const b = buttons.nth(i)
      const box = await targetBox(b)
      expect(box.height).toBeGreaterThanOrEqual(44)
      expect(box.width).toBeGreaterThanOrEqual(44)
      expect(await b.evaluate((el) => getComputedStyle(el).opacity)).toBe('1')
    }
  })
})

test.describe('Changes tab comment gutter — desktop mouse', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('stays a dense 20px gutter for a hover-capable pointer', async ({ page }) => {
    await openChanges(page)
    const box = await targetBox(page.getByRole('button', { name: 'Comment on line 2' }))
    expect(box.height).toBeLessThan(24)
    expect(box.width).toBeLessThan(24)
  })
})
