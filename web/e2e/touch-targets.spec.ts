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

  test('search input and ordering selects meet the 44px target', async ({ page }) => {
    await interceptApi(page)
    await page.goto('/sessions')
    await expect(page.getByTestId('session-list-controls')).toBeVisible()

    const search = await targetBox(page.getByLabel('Search sessions'))
    expect(search.height).toBeGreaterThanOrEqual(44)

    const sortBy = await targetBox(page.getByLabel('Sort by'))
    expect(sortBy.height).toBeGreaterThanOrEqual(44)

    const status = await targetBox(page.getByLabel('Status', { exact: true }))
    expect(status.height).toBeGreaterThanOrEqual(44)
  })

  test('"Clear filters" meets the 44px target once a filter is applied', async ({ page }) => {
    await interceptApi(page)
    await page.goto('/sessions')
    await page.getByLabel('Status', { exact: true }).selectOption({ label: 'Working' })

    const clear = await targetBox(page.getByRole('button', { name: 'Clear filters' }))
    expect(clear.height).toBeGreaterThanOrEqual(44)
  })
})

test.describe('Sessions list-controls touch targets — desktop mouse', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('stays compact for a hover-capable pointer', async ({ page }) => {
    await interceptApi(page)
    await page.goto('/sessions')

    const sortBy = await targetBox(page.getByLabel('Sort by'))
    // Not a regression threshold on the exact px — just proving the
    // touch-target floor does not leak into the dense desktop chrome.
    expect(sortBy.height).toBeLessThan(36)
  })
})
