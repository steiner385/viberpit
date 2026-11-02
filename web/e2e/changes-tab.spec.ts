/**
 * The session Changes tab (D-4): the header's `+N −M` chip opens it, it renders
 * the worktree diff, and neither the tab nor the page scrolls sideways at phone,
 * tablet or desktop widths (spec 2026-11-01 §12). The diff API is mocked with
 * `page.route`, like the other session specs.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'changes-tab'

const LONG = 'export const somethingFairlyLongToForceTheUnifiedRowWiderThanAPhone = computeTheValue(alpha, beta, gamma, delta)'

const DIFF = {
  base: '0123456789abcdef0123456789abcdef01234567',
  head: 'fedcba9876543210fedcba9876543210fedcba98',
  additions: 3,
  deletions: 1,
  truncated: false,
  files: [
    {
      path: 'web/src/features/sessions/a/really/deeply/nested/directory/structure/component.tsx',
      oldPath: null,
      status: 'modified',
      additions: 2,
      deletions: 1,
      binary: false,
      tooLarge: false,
      hunks: [{
        oldStart: 1, oldLines: 3, newStart: 1, newLines: 4, header: '',
        lines: [
          { kind: 'context', oldNo: 1, newNo: 1, text: 'import { x } from "y"' },
          { kind: 'del', oldNo: 2, newNo: null, text: 'const a = 1' },
          { kind: 'add', oldNo: null, newNo: 2, text: 'const a = 2' },
          { kind: 'add', oldNo: null, newNo: 3, text: LONG },
          { kind: 'context', oldNo: 3, newNo: 4, text: 'export default a' },
        ],
      }],
    },
    { path: 'logo.png', oldPath: null, status: 'added', additions: 1, deletions: 0, binary: true, tooLarge: false, hunks: [] },
  ],
}

async function openSession(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Changes tab', accountName: 'alice', engine: 'claude_code', status: 'idle',
    createdAt: '2026-11-01T10:00:00Z', updatedAt: '2026-11-01T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => {
    const summary = new URL(route.request().url()).searchParams.get('summary') === '1'
    route.fulfill({ json: summary ? { ...DIFF, files: DIFF.files.map((f) => ({ ...f, hunks: [] })) } : DIFF })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    if (JSON.parse(msg.toString()).type === 'attach') ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session changes tab/i }).click()
}

async function noOverflow(page: Page) {
  const panel = page.locator('#tab-panel-changes')
  const p = await panel.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
  expect(p.scrollWidth).toBeLessThanOrEqual(p.clientWidth)
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
}

for (const width of [390, 820, 1440]) {
  test(`the chip opens the Changes tab, which fits at ${width}px`, async ({ page }) => {
    await openSession(page, width)
    const chip = page.getByRole('button', { name: /3 additions, 1 deletion — open changes/i })
    await expect(chip).toBeVisible()
    await chip.click()
    await expect(page).toHaveURL(/view=changes/)
    await expect(page.getByRole('tab', { name: 'Changes' })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByText('const a = 2')).toBeVisible()
    await expect(page.getByText(/binary file/i)).toBeVisible()
    // Below 1024px the default is unified; from 1024px, split.
    await expect(page.getByRole('button', { name: width >= 1024 ? 'Split' : 'Unified' }))
      .toHaveAttribute('aria-pressed', 'true')
    await noOverflow(page)
  })
}

test('Next change focuses the first change run', async ({ page }) => {
  await openSession(page, 1440)
  await page.getByRole('tab', { name: 'Changes' }).click()
  await page.getByRole('button', { name: 'Next change' }).click()
  await expect(page.locator('[data-diff-row]:focus')).toContainText('const a = 1')
})
