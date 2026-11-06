/**
 * D-21 follow-ups, route-mocked at 1440 and 390:
 *  - an archived session's page lists its recovery snapshots (reason, short SHA,
 *    date) with the restore command as literal text that a click puts on the
 *    clipboard exactly; a refusal (409 `daemon_outdated`) or no refs shows no card;
 *  - ⌘K "New session on repo@branch" offers the repo's real branches: nothing is
 *    fetched when the palette opens, typing `<repo>@` fetches that one repo, the
 *    default branch comes first, and a failed fetch keeps the live branches quietly.
 *
 * Run: npx playwright test e2e/d21-followups.spec.ts
 */
import { test, expect, type Page } from '@playwright/test'

const ARCHIVED = {
  id: 'arc-21', name: 'Discarded experiment', account: 'alice', cwd: '/tmp/x', repo: 'acme/rocket',
  engine: 'claude_code', engineSessionId: 'c-1', permissionMode: 'default',
  createdAt: '2026-11-01T10:00:00Z', archivedAt: '2026-11-05T10:00:00Z',
}
const RESTORE = 'git -C /home/u/repos/acme-rocket worktree add ../restored "refs/viberpit/recovery/arc-21/discard" <b>x</b>'
const REFS = {
  refs: [
    { ref: 'refs/viberpit/recovery/arc-21/discard', reason: 'discard', sha: '0123456789abcdef', createdAt: '2026-11-05T09:59:00Z' },
    { ref: 'refs/viberpit/recovery/arc-21/close', reason: 'close', sha: 'fedcba9876543210', createdAt: null },
  ],
  restoreCommand: RESTORE,
}

const LIVE = [
  { id: 'd21-a', name: 'Refactor the rocket', repo: 'acme/rocket', branch: 'feat/thrust', updatedAt: '2026-11-02T10:00:00Z' },
  { id: 'd21-b', name: 'Polish the site', repo: 'acme/site', branch: 'main', updatedAt: '2026-11-01T10:00:00Z' },
]
const REPOS = ['acme/rocket', 'acme/site'].map((nameWithOwner) => ({
  nameWithOwner, name: nameWithOwner.split('/')[1], owner: 'acme', isPrivate: false, url: '',
  defaultBranch: 'main', pushedAt: null, cloned: true,
}))

async function baseRoutes(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 })
  // Registered first: Playwright tries the LAST registered route first.
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage(() => {}))
}

async function noSidewaysScroll(page: Page) {
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
}

async function openArchived(page: Page, width: number, refs: { status: number; json: unknown }) {
  await baseRoutes(page, width)
  await page.route('**/api/sessions/*/transcript*', (route) => route.fulfill({ json: {
    events: [
      { type: 'user', turnId: 'u0', text: 'Try the thing' },
      { type: 'turnStart', turnId: 'a0' },
      { type: 'textDelta', turnId: 'a0', text: 'Tried it' },
      { type: 'turnEnd', turnId: 'a0' },
    ],
    earliestTurn: 0, totalTurns: 2,
  } }))
  await page.route('**/api/sessions/archived*', (route) => route.fulfill({ json: [ARCHIVED] }))
  await page.route(`**/api/sessions/${ARCHIVED.id}/recovery-refs`, (route) => route.fulfill(refs))
  await page.goto(`/sessions/archived/${ARCHIVED.id}`)
  await expect(page.getByRole('heading', { level: 1, name: ARCHIVED.name })).toBeVisible()
  await expect(page.getByText('Tried it')).toBeVisible()
}

async function openPalette(page: Page, width: number, branches: { status: number; json: unknown }) {
  await baseRoutes(page, width)
  await page.route('**/api/sessions', (route) => route.fulfill({
    json: LIVE.map((s) => ({
      ...s, accountName: 'alice', engine: 'claude_code', status: 'idle', createdAt: '2026-11-01T09:00:00Z', prs: [],
    })),
  }))
  await page.route('**/api/github/repos', (route) => route.fulfill({ json: REPOS }))
  const requested: string[] = []
  await page.route('**/api/github/branches**', (route) => {
    requested.push(new URL(route.request().url()).searchParams.get('repo') ?? '')
    return route.fulfill(branches)
  })
  await page.goto('/sessions')
  await expect(page.getByRole('button', { name: /open session refactor the rocket/i })).toBeVisible()
  await page.keyboard.press('Control+KeyK')
  const palette = page.getByRole('dialog', { name: 'Command palette' })
  await expect(palette.getByRole('combobox')).toBeFocused()
  await expect(palette.getByRole('option').first()).toBeVisible()
  return { palette, requested }
}

for (const width of [1440, 390]) {
  test.describe(`D-21 at ${width}px`, () => {
    test('an archived session lists its recovery snapshots; clicking the command copies it exactly', async ({ page, context }) => {
      await context.grantPermissions(['clipboard-read', 'clipboard-write'])
      await openArchived(page, width, { status: 200, json: REFS })
      const card = page.getByRole('region', { name: 'Recovery snapshots' })
      await expect(card).toBeVisible()
      const items = card.getByRole('listitem')
      await expect(items).toHaveCount(2)
      await expect(items.nth(0)).toContainText('discard')
      await expect(items.nth(0)).toContainText('0123456')
      await expect(items.nth(1)).toContainText('close')
      await expect(items.nth(1)).toContainText('fedcba9')
      // Literal text: the `<b>` is characters, not an element.
      const restore = card.getByTitle('Copy restore command')
      await expect(restore).toHaveText(RESTORE)
      await expect(card.locator('b')).toHaveCount(0)
      await restore.click()
      await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(RESTORE)
      await noSidewaysScroll(page)
    })

    test('a refusal or no refs shows no card, and the transcript is unaffected', async ({ page }) => {
      await openArchived(page, width, { status: 409, json: { detail: { code: 'daemon_outdated' } } })
      await expect(page.getByRole('region', { name: 'Recovery snapshots' })).toHaveCount(0)
      await page.unroute(`**/api/sessions/${ARCHIVED.id}/recovery-refs`)
      await page.route(`**/api/sessions/${ARCHIVED.id}/recovery-refs`,
        (route) => route.fulfill({ json: { refs: [], restoreCommand: null } }))
      await page.reload()
      await expect(page.getByText('Tried it')).toBeVisible()
      await expect(page.getByRole('region', { name: 'Recovery snapshots' })).toHaveCount(0)
    })

    test('⌘K fetches one repo’s branches only once it is focused, default first', async ({ page }) => {
      const { palette, requested } = await openPalette(page, width, {
        status: 200, json: { default: 'main', branches: ['dev', 'feat/thrust', 'main'] },
      })
      expect(requested).toEqual([])
      await page.keyboard.type('acme/rocket@')
      const branches = palette.getByRole('group', { name: 'Branches' }).getByRole('option')
      await expect(branches).toHaveText([
        'New session on acme/rocket@main', 'New session on acme/rocket@dev', 'New session on acme/rocket@feat/thrust',
      ])
      expect(requested).toEqual(['acme/rocket'])
      const box = await palette.boundingBox()
      expect(box!.x + box!.width).toBeLessThanOrEqual(width)
      await noSidewaysScroll(page)
    })

    test('⌘K keeps the live branches when the fetch fails, with no error toast', async ({ page }) => {
      const { palette, requested } = await openPalette(page, width, { status: 502, json: { detail: 'GitHub down' } })
      await page.keyboard.type('acme/rocket@')
      await expect.poll(() => requested.length).toBeGreaterThan(0)
      await expect(palette.getByRole('group', { name: 'Branches' }).getByRole('option'))
        .toHaveText(['New session on acme/rocket@feat/thrust'])
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    })
  })
}
