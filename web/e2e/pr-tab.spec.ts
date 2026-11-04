/**
 * The session PR tab (D-15): listed for a session with PRs, it shows the selected
 * PR's header, grouped checks and review threads; "Ask session to address" prefills
 * the composer without sending; Merge now confirms, posts `{method: "squash"}`, and a
 * merged PR keeps only Open on GitHub. A hostile thread body stays inert, and the tab
 * fits a phone and a desktop width without sideways scroll. The API is mocked with
 * `page.route`, like the other session specs.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'pr-tab'
const REPO = 'acme/widgets'

const PR = {
  number: 42, url: `https://github.com/${REPO}/pull/42`, state: 'open', checks: 'failing',
  title: 'Add the widget registry and wire it through the session pane', deployed: false, repo: REPO,
  is_draft: false, merge_state_status: 'CLEAN', auto_merge_enabled: false, in_merge_queue: false, labels: [],
}

const THREADS = [
  { id: 'R1', path: 'src/old.ts', line: 4, resolved: true, outdated: false,
    comments: [{ author: 'bob', body: 'Done, thanks.', createdAt: '2026-11-01T00:00:00Z', url: 'https://github.com/x#r0' }] },
  { id: 'U1', path: 'web/src/features/sessions/pr/a/really/deeply/nested/path/PrTab.tsx', line: 120, resolved: false, outdated: false,
    comments: [{ author: 'alice', body: 'Please **rename** this — `fooBarBazQuxQuuxCorgeGraultGarplyWaldoFredPlughXyzzyThud` is unclear.',
                 createdAt: '2026-11-01T00:00:00Z', url: 'https://github.com/x#r1' }] },
  { id: 'U2', path: 'src/evil.ts', line: 9, resolved: false, outdated: true,
    comments: [{ author: 'mallory', body: 'Hi <script>window.__pwned = 1</script> [click me](javascript:window.__pwned=2) <img src=x onerror="window.__pwned=3">',
                 createdAt: '2026-11-01T00:00:00Z', url: 'javascript:window.__pwned=4' }] },
]

function detail(over: Record<string, unknown> = {}) {
  return {
    ...PR,
    checks: [
      { name: 'lint-and-test', status: 'COMPLETED', conclusion: 'failure', url: 'https://github.com/x/runs/1' },
      { name: 'frontend', status: 'IN_PROGRESS', conclusion: null, url: 'https://github.com/x/runs/2' },
      { name: 'e2e', status: 'COMPLETED', conclusion: 'success', url: 'https://github.com/x/runs/3' },
    ],
    reviews: [], threads: THREADS,
    isDraft: false, autoMergeEnabled: false, mergeStateStatus: 'CLEAN', humanHold: false,
    ...over,
  }
}

async function openSession(page: Page, width: number, opts: { hold?: boolean } = {}) {
  await page.setViewportSize({ width, height: 900 })
  let merged = false
  const posts: { url: string; body: unknown }[] = []
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'PR tab', accountName: 'alice', engine: 'claude_code', status: 'idle',
    cwd: '/home/u/worktrees/pr', prs: [PR, { ...PR, number: 40, state: 'merged', title: 'Older merged PR' }],
    createdAt: '2026-11-02T10:00:00Z', updatedAt: '2026-11-02T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route(`**/api/sessions/${sid}/files**`, (route) => {
    const q = new URL(route.request().url()).searchParams.get('q') ?? ''
    return route.fulfill({ json: { files: q === THREADS[1].path ? [{ path: q }] : [], truncated: false } })
  })
  await page.route(`**/api/prs/${REPO}/**`, async (route) => {
    const req = route.request()
    if (req.method() === 'POST') {
      posts.push({ url: req.url(), body: req.postDataJSON() })
      if (req.url().endsWith('/merge')) merged = true
      return route.fulfill({ json: { ok: true, error: '' } })
    }
    return route.fulfill({ json: merged
      ? detail({ state: 'merged', merged: true, mergeStateStatus: 'UNKNOWN' })
      : detail(opts.hold ? { humanHold: true, labels: ['hold:human'] } : {}) })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string }
    if (frame.type === 'attach') ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session pr tab/i }).click()
  await page.getByRole('tab', { name: 'PR' }).click()
  await expect(page).toHaveURL(/view=pr/)
  return { posts }
}

async function noOverflow(page: Page) {
  const panel = page.locator('#tab-panel-pr')
  const p = await panel.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
  expect(p.scrollWidth).toBeLessThanOrEqual(p.clientWidth)
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
}

for (const width of [390, 1440]) {
  test(`header, grouped checks and threads, inert hostile body, and it fits at ${width}px`, async ({ page }) => {
    await openSession(page, width)
    const header = page.getByRole('region', { name: 'Pull request #42' })
    await expect(header.getByRole('heading', { name: PR.title })).toBeVisible()
    await expect(header.getByText('Ready to merge')).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'Pull request' })).toHaveValue(`${REPO}#42`)
    await expect(page.getByText('Failing (1)')).toBeVisible()
    await expect(page.getByText('lint-and-test')).toBeVisible()
    await expect(page.getByText('1 resolved')).toBeVisible()
    await expect(page.getByText('Outdated')).toBeVisible()
    // Wait for the lazy markdown renderer (its fallback is the raw text), then the
    // hostile body: the link text renders, but no script ran and no javascript: href exists.
    await expect(page.locator('#tab-panel-pr strong', { hasText: 'rename' })).toBeVisible()
    await expect(page.getByText('click me', { exact: true })).toBeVisible()
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined()
    expect(await page.locator('#tab-panel-pr a[href^="javascript:" i]').count()).toBe(0)
    expect(await page.locator('#tab-panel-pr script').count()).toBe(0)
    await expect(page.getByRole('group', { name: 'Pull request actions' }).getByRole('button'))
      .toHaveText(['Ready + auto-merge', 'Merge now', 'Fix'])
    await noOverflow(page)
    await page.screenshot({ path: `/tmp/d15-pr-tab-${width}.png`, fullPage: true })
  })
}

test('the thread path opens the Files tab at its line', async ({ page }) => {
  await openSession(page, 1440)
  await page.getByRole('button', { name: `${THREADS[1].path}:120` }).click()
  await expect(page).toHaveURL(/view=files/)
  await expect(page).toHaveURL(/line=120/)
})

test('"Ask session to address" prefills the composer and shows the conversation, without sending', async ({ page }) => {
  const { posts } = await openSession(page, 390)
  await page.getByRole('article', { name: `${THREADS[1].path}:120` })
    .getByRole('button', { name: 'Ask session to address' }).click()
  await expect(page.getByRole('tab', { name: 'Conversation' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('textbox', { name: /^message$/i }))
    .toHaveValue(`Address this review comment on ${THREADS[1].path}:120:\n\n> ${THREADS[1].comments[0].body}`)
  expect(posts).toEqual([])
})

test('Merge now confirms, posts squash, and a merged PR keeps only Open on GitHub', async ({ page }) => {
  const { posts } = await openSession(page, 1440)
  await page.getByRole('button', { name: 'Merge now' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Merge' }).click()
  await expect.poll(() => posts.map((p) => [new URL(p.url).pathname, p.body]))
    .toEqual([[`/api/prs/${REPO}/42/merge`, { method: 'squash' }]])
  const header = page.getByRole('region', { name: 'Pull request #42' })
  await expect(header.getByText('Merged', { exact: true })).toBeVisible()
  await expect(page.getByRole('group', { name: 'Pull request actions' })).toHaveCount(0)
  await expect(header.getByRole('link', { name: /Open on GitHub/ })).toBeVisible()
})

test('a held PR hides merge, arm and Fix', async ({ page }) => {
  await openSession(page, 1440, { hold: true })
  const actions = page.getByRole('group', { name: 'Pull request actions' })
  await expect(actions.getByText('Held for a human — merge it on GitHub')).toBeVisible()
  await expect(actions.getByRole('button')).toHaveCount(0)
})
