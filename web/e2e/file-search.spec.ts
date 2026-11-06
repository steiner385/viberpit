/**
 * The Files tab's content search (D-20): Contents switches the list to a search of
 * the worktree's contents, results group by file with the match marked, and a
 * result (clicked, or Enter on the active one) opens the viewer at its line. A
 * match is file content, so an HTML payload renders as text. Neither the tab nor
 * the page scrolls sideways at a phone or a desktop width. The API is mocked with
 * `page.route`, like `files-tab.spec.ts`.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'file-search'

const DEEP = 'web/src/features/sessions/a/really/deeply/nested/directory/structure/component.tsx'
const PAYLOAD = '<img src=x onerror="window.__pwned=1"> const needle = 1'
const LONG = `export const needle = ${'computeTheValue(alpha, beta, gamma, delta) + '.repeat(5)}0`
const TEXT = Array.from({ length: 200 }, (_, i) => `const line${i + 1} = ${i + 1}`).join('\n') + '\n'

const MATCHES = [
  { path: DEEP, line: 120, text: LONG, start: LONG.indexOf('needle'), end: LONG.indexOf('needle') + 6 },
  { path: DEEP, line: 150, text: 'const line150 = needle', start: 16, end: 22 },
  { path: 'evil.html', line: 7, text: PAYLOAD, start: PAYLOAD.indexOf('needle'), end: PAYLOAD.indexOf('needle') + 6 },
]

async function openSession(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'File search', accountName: 'alice', engine: 'claude_code', status: 'idle',
    cwd: '/home/u/worktrees/search',
    createdAt: '2026-11-06T10:00:00Z', updatedAt: '2026-11-06T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route(`**/api/sessions/${sid}/files**`, (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/files/content')) {
      const path = url.searchParams.get('path') ?? ''
      return route.fulfill({ json: { path, text: TEXT, binary: false, truncated: false, size: TEXT.length } })
    }
    if (url.pathname.endsWith('/files/search')) {
      const q = url.searchParams.get('q') ?? ''
      if (url.searchParams.get('regex') === 'true' && q.includes('(')) {
        return route.fulfill({ status: 400, json: { detail: { code: 'BAD_REGEX' } } })
      }
      return route.fulfill({ json: q === 'needle'
        ? { matches: MATCHES, truncated: true, timedOut: false }
        : { matches: [], truncated: false, timedOut: false } })
    }
    return route.fulfill({ json: { files: [{ path: DEEP }, { path: 'evil.html' }], truncated: false } })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string }
    if (frame.type === 'attach') ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session file search/i }).click()
  await page.getByRole('tab', { name: 'Files' }).click()
  await page.getByRole('button', { name: 'Contents' }).click()
}

async function noOverflow(page: Page) {
  const panel = page.locator('#tab-panel-files')
  const p = await panel.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
  expect(p.scrollWidth).toBeLessThanOrEqual(p.clientWidth)
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
}

for (const width of [390, 1440]) {
  test(`search contents, open a result at its line, and it fits at ${width}px`, async ({ page }) => {
    await openSession(page, width)
    const box = page.getByRole('combobox', { name: 'Search file contents' })
    await expect(page.getByText(/Type at least 2 characters/)).toBeVisible()
    await box.fill('needle')
    const results = page.getByRole('listbox', { name: 'Search results' })
    await expect(results.getByRole('option')).toHaveCount(3)
    await expect(page.getByText('Showing first 3 matches')).toBeVisible()
    await expect(page.getByRole('group', { name: DEEP }).getByRole('option')).toHaveCount(2)
    // The payload is text: no image, nothing ran.
    const evil = page.getByRole('group', { name: 'evil.html' }).getByRole('option')
    await expect(evil).toContainText('<img src=x onerror=')
    await expect(evil.locator('mark')).toHaveText('needle')
    await expect(page.locator('#tab-panel-files img')).toHaveCount(0)
    expect(await page.evaluate(() => (window as { __pwned?: number }).__pwned)).toBeUndefined()
    await noOverflow(page)

    // ↓ to the second result, Enter opens it at line 150.
    await box.press('ArrowDown')
    await box.press('Enter')
    await expect(page).toHaveURL(/line=150/)
    const target = page.locator('[data-line="150"]')
    await expect(target).toBeInViewport()
    await expect(target).toHaveAttribute('data-target', '')
    await noOverflow(page)

    // A pane under 48rem (a phone; the desktop with the rail beside it) shows the file
    // in place of the results, and All files goes back to them.
    const back = page.getByRole('button', { name: 'All files' })
    if (width === 390) await expect(back).toBeVisible()
    if (await back.isVisible()) await back.click()
    await expect(results.getByRole('option')).toHaveCount(3)
    // A click opens the file at its line too.
    await evil.click()
    await expect(page).toHaveURL(/file=evil\.html/)
    await expect(page).toHaveURL(/line=7/)
    await expect(page.locator('[data-line="7"]')).toHaveAttribute('data-target', '')
  })
}

test('a rejected regex says so inline, and an empty search says No matches', async ({ page }) => {
  await openSession(page, 1440)
  const box = page.getByRole('combobox', { name: 'Search file contents' })
  await page.getByRole('button', { name: 'Regex' }).click()
  await box.fill('foo(')
  await expect(page.getByText('Invalid regular expression')).toBeVisible()
  await box.fill('zzz')
  await expect(page.getByText('No matches')).toBeVisible()
})
