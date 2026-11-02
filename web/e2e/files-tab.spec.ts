/**
 * The session Files tab (D-7): listed for a session with a cwd, it searches the
 * worktree, opens a file in the read-only viewer, and `?file=&line=` lands on a
 * highlighted, focused line. Neither the tab nor the page scrolls sideways at a
 * phone or a desktop width — a long line scrolls inside the viewer's own frame.
 * The files API is mocked with `page.route`, like the other session specs.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'files-tab'

const PATH = 'web/src/features/sessions/a/really/deeply/nested/directory/structure/component.tsx'
const LONG = 'export const somethingFairlyLongToForceTheLineWiderThanAPhone = computeTheValue(alpha, beta, gamma, delta, epsilon)'
const TEXT = Array.from({ length: 200 }, (_, i) => (i === 2 ? LONG : `const line${i + 1} = ${i + 1}`)).join('\n') + '\n'

const FILES = [{ path: PATH }, { path: 'README.md' }, { path: 'logo.png' }]

async function openSession(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Files tab', accountName: 'alice', engine: 'claude_code', status: 'idle',
    cwd: '/home/u/worktrees/files',
    createdAt: '2026-11-02T10:00:00Z', updatedAt: '2026-11-02T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route(`**/api/sessions/${sid}/files**`, (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/files/content')) {
      const path = url.searchParams.get('path') ?? ''
      return path === 'logo.png'
        ? route.fulfill({ json: { path, text: null, binary: true, truncated: false, size: 2048 } })
        : route.fulfill({ json: { path, text: TEXT, binary: false, truncated: false, size: TEXT.length } })
    }
    const q = (url.searchParams.get('q') ?? '').toLowerCase()
    return route.fulfill({ json: { files: FILES.filter((f) => f.path.toLowerCase().includes(q)), truncated: false } })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string }
    if (frame.type === 'attach') ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session files tab/i }).click()
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
  test(`search, open a file, and it fits at ${width}px`, async ({ page }) => {
    await openSession(page, width)
    await page.getByRole('tab', { name: 'Files' }).click()
    await expect(page).toHaveURL(/view=files/)
    await page.getByRole('searchbox', { name: 'Search files' }).fill('component')
    const hit = page.getByRole('button', { name: /component\.tsx/ })
    await expect(hit).toBeVisible()
    await expect(page.getByRole('button', { name: /README\.md/ })).toHaveCount(0)
    await noOverflow(page)
    await hit.click()
    await expect(page).toHaveURL(/file=web%2Fsrc/)
    await expect(page.locator('[data-line="3"]')).toContainText('somethingFairlyLong')
    await noOverflow(page)
    // The long line scrolls inside the viewer's frame, not the page.
    const frame = page.getByRole('region', { name: `Contents of ${PATH}` })
    if (width === 390) {
      const f = await frame.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
      expect(f.scrollWidth).toBeGreaterThan(f.clientWidth)
      // Narrow: the open file replaces the list; All files goes back to it.
      await page.getByRole('button', { name: 'All files' }).click()
      await expect(page.getByRole('searchbox', { name: 'Search files' })).toBeVisible()
    }
  })

  test(`?file=&line=150 lands on a highlighted, focused line 150 at ${width}px`, async ({ page }) => {
    await openSession(page, width)
    const url = new URL(page.url())
    url.searchParams.set('view', 'files')
    url.searchParams.set('file', PATH)
    url.searchParams.set('line', '150')
    await page.goto(url.toString())
    const target = page.locator('[data-line="150"]')
    await expect(target).toBeInViewport()
    await expect(target).toHaveAttribute('data-target', '')
    await expect(target).toBeFocused()
    await expect(target).toContainText('const line150 = 150')
    // Windowed: 200 lines, far fewer rows mounted.
    expect(await page.locator('[data-line]').count()).toBeLessThan(200)
    await noOverflow(page)
  })
}

test('a binary file says so', async ({ page }) => {
  await openSession(page, 1440)
  await page.getByRole('tab', { name: 'Files' }).click()
  await page.getByRole('button', { name: /logo\.png/ }).click()
  await expect(page.getByText('Binary file — not shown')).toBeVisible()
})
