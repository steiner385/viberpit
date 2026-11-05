/**
 * "Move to host…" (D-17), route-mocked like the other session specs: the ⋯ menu item,
 * the dialog's preflight-driven states (private-repo consent, public-repo refusal),
 * the outcome, and the "Moving to <host>…" chip + disabled composer while `movingTo`
 * is set. Host names and file paths are deliberately long — they are untrusted and
 * must truncate rather than push a 390px viewport sideways.
 *
 * Run: npx playwright test e2e/move-session.spec.ts
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'move-session'
const LONG_HOST = 'rocky-with-an-extraordinarily-long-runner-hostname.tailnet-example.ts.net'
const LONG_PATH = 'src/features/some/deeply/nested/directory/structure/with/a/really-long-file-name-that-keeps-going.tsx'

interface Opts {
  movingTo?: string | null
  preflight?: Record<string, unknown>
  hosts?: string[]
}

const fleetHost = (name: string) => ({
  name, endpoint: `https://${name}:8790`, enabled: true, weight: 1, maxSessions: null, healthy: true,
  activeSessions: 0, loadAvg: null, memAvailableMb: null, sha: null, checkoutSha: null, protocol: null,
  lastProbeAt: null, lastError: null, sshHost: null, repoPath: null, upgrade: null,
})

const okPreflight = (over: Record<string, unknown> = {}) => ({
  sessionId: sid, source: 'dobby', target: LONG_HOST,
  hosts: [{ name: LONG_HOST, ok: true, reason: null, message: '' }],
  dirty: { count: 0, paths: [] }, repoPrivate: null, backgroundTasks: [], factsError: null,
  verdict: { ok: true, reason: null, message: '' }, ...over,
})

async function openSession(page: Page, width: number, opts: Opts = {}) {
  await page.setViewportSize({ width, height: 900 })
  const posts: unknown[] = []
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Move me', accountName: 'alice', engine: 'claude_code', status: 'idle',
    host: 'dobby', movingTo: opts.movingTo ?? null,
    branch: 'feat/move', cwd: '/home/u/worktrees/move',
    createdAt: '2026-11-05T10:00:00Z', updatedAt: '2026-11-05T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/api/fleet/hosts', (route) => route.fulfill({
    json: { hosts: (opts.hosts ?? ['dobby', LONG_HOST]).map(fleetHost) },
  }))
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route(`**/api/sessions/${sid}/move/preflight**`, (route) => route.fulfill({
    json: opts.preflight ?? okPreflight(),
  }))
  await page.route(`**/api/sessions/${sid}/move`, async (route) => {
    posts.push(route.request().postDataJSON())
    await route.fulfill({ json: {
      sessionId: sid, from: 'dobby', to: LONG_HOST, outcome: 'moved', finalized: true, recoveryRef: null, session: null,
    } })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string }
    if (frame.type === 'attach') ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session move me/i }).click()
  return { posts }
}

/** Neither the page nor an open dialog scrolls sideways. */
async function noOverflow(page: Page) {
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
  const dialog = page.getByRole('dialog')
  if (await dialog.count()) {
    const d = await dialog.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
    expect(d.scrollWidth).toBeLessThanOrEqual(d.clientWidth)
  }
}

async function openMoveDialog(page: Page) {
  await page.getByRole('button', { name: 'More session actions' }).click()
  await page.getByRole('menuitem', { name: 'Move to host…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Move to host' })
  await expect(dialog).toBeVisible()
  return dialog
}

for (const width of [1440, 390]) {
  test(`${width}px: a dirty private repo needs consent, then moves`, async ({ page }) => {
    const { posts } = await openSession(page, width, {
      preflight: okPreflight({
        dirty: { count: 14, paths: [LONG_PATH, ...Array.from({ length: 13 }, (_, i) => `src/f${i}.ts`)] },
        repoPrivate: true,
        backgroundTasks: [{ kind: 'monitor', name: 'Monitor', description: 'tail the CI log', replayable: true }],
      }),
    })
    const dialog = await openMoveDialog(page)
    const select = dialog.getByRole('combobox', { name: 'Target host' })
    // The current host is not a target.
    await expect(select.locator('option')).toHaveText(['Choose a host…', LONG_HOST])
    await select.selectOption(LONG_HOST)

    await expect(dialog.getByTestId('move-dirty-paths')).toContainText('+6 more')
    await expect(dialog.getByTestId('move-dirty-paths')).toContainText(LONG_PATH)
    const move = dialog.getByRole('button', { name: 'Move', exact: true })
    await expect(move).toBeDisabled()
    await dialog.getByRole('checkbox', {
      name: 'Carry uncommitted changes (they are pushed to a hidden ref on GitHub and deleted after the move)',
    }).check()
    await expect(move).toBeDisabled()
    await dialog.getByRole('checkbox', { name: 'Stop these tasks and move' }).check()
    await expect(move).toBeEnabled()
    await noOverflow(page)
    await page.screenshot({ path: `/tmp/d17-${width}.png` })

    await move.click()
    await expect(dialog.getByTestId('move-outcome')).toContainText(`Moved to ${LONG_HOST}`)
    expect(posts).toEqual([{ host: LONG_HOST, carryDirty: true, stopBackgroundTasks: true }])
    await noOverflow(page)
  })

  test(`${width}px: a dirty public repo is refused, even with consent`, async ({ page }) => {
    await openSession(page, width, {
      preflight: okPreflight({
        dirty: { count: 1, paths: ['README.md'] }, repoPrivate: false,
        verdict: { ok: false, reason: 'MOVE_DIRTY_PUBLIC_REPO', message: 'refused' },
      }),
    })
    const dialog = await openMoveDialog(page)
    await dialog.getByRole('combobox', { name: 'Target host' }).selectOption(LONG_HOST)
    await expect(dialog.getByTestId('move-refusal'))
      .toHaveText('This repository is public — commit or stash your changes before moving')
    await expect(dialog.getByRole('checkbox')).toHaveCount(0)
    await expect(dialog.getByRole('button', { name: 'Move', exact: true })).toBeDisabled()
  })

  test(`${width}px: while moving, the chip shows and the composer is held`, async ({ page }) => {
    await openSession(page, width, { movingTo: LONG_HOST })
    const header = page.locator('[data-region="session.header"]')
    await expect(header.getByTestId('moving-chip')).toHaveAttribute('title', `Moving to ${LONG_HOST}…`)
    await expect(page.getByRole('button', { name: 'Moving…' })).toBeDisabled()
    await noOverflow(page)
    await page.screenshot({ path: `/tmp/d17-${width}-moving.png` })
  })
}

test('a one-host fleet offers no move', async ({ page }) => {
  await openSession(page, 1440, { hosts: ['dobby'] })
  await page.getByRole('button', { name: 'More session actions' }).click()
  await expect(page.getByRole('menu', { name: 'Session actions' })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: /move to host/i })).toHaveCount(0)
})
