/**
 * D-19 parity leftovers, route-mocked at 1440 and 390:
 *  - an error card's recovery actions by error `kind` — Switch model opens the prompt
 *    row's picker, Lower effort posts one level down, a missing model offers no retry;
 *  - the Environment card's worktree Discard (confirm with counts, branch, restore hint
 *    and the ignored-files warning; leaves the session; the refs ride a toast) and
 *    Repair (only for a missing worktree);
 *  - ⌘K "New session on repo@branch": the dispatch bar opens on that repo with a branch
 *    chip, the create sends the branch, and a 409 `branch_switch_failed` is shown.
 *
 * Run: npx playwright test e2e/parity-leftovers.spec.ts
 */
import { test, expect, type Page, type Request } from '@playwright/test'
import { replayFrame, userTurn, assistantTurn, wire } from './fixtures/wsFrames'
import type { SessionEvent } from '../src/session/events'

const sid = 'parity-session'
const U1 = '11111111-1111-4111-8111-111111111111'
const CWD = `/home/u/worktrees/${sid}`

const CAPS = { models: true, reasoningEffort: true, steer: true, interrupt: true, approvals: true,
  questions: true, adopt: true, selfModelSwitch: true, subagents: true, attachments: true,
  accountRotation: true, accountSwitchNewThread: false, fork: false, rewind: true, flagSettings: true }

const PREFLIGHT = {
  sessionId: sid, path: CWD, managed: true, present: true, state: 'present',
  branch: 'viberpit/parity-session', sessionBranch: true, dirty: 3, unpushed: 2, recoveryRefs: [],
  restoreHint: 'git -C /home/u/repos/acme-rocket worktree add <dir> <recovery-ref>',
  busy: false, canDiscard: true, canRepair: false,
}
const DISCARDED = {
  discarded: true,
  recoveryRefs: ['refs/viberpit/recovery/parity-session-discard', 'refs/viberpit/recovery/parity-session-tip'],
  restoreCommand: 'git -C /home/u/repos/acme-rocket worktree add ../parity refs/viberpit/recovery/parity-session-discard',
  path: CWD, branch: 'viberpit/parity-session', branchDeleted: true,
}
const IGNORED = 'Files git ignores (such as .env and build output) are not saved — they are deleted with the worktree.'

const failed = (kind: string, code: string, message: string): SessionEvent[] => [
  ...userTurn(U1, 'Fix the flaky test'),
  ...assistantTurn('t1', 'Looking into it').slice(0, -1),
  { type: 'error', turnId: 't1', code, message, kind } as SessionEvent,
  { type: 'turnEnd', turnId: 't1' },
]

interface Opts { history?: SessionEvent[]; preflight?: Record<string, unknown>; effort?: string }

async function openSession(page: Page, width: number, opts: Opts = {}) {
  const { history = failed('rate_limit', 'api_429', 'Rate limited — try again shortly'), preflight = PREFLIGHT } = opts
  const posts: Request[] = []
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Parity me', accountName: 'alice', engine: 'claude_code', status: 'idle', model: 'opus',
    reasoningEffort: opts.effort ?? 'high', supportedEffortLevels: ['low', 'medium', 'high'],
    repo: 'acme/rocket', branch: 'viberpit/parity-session', cwd: CWD,
    createdAt: '2026-11-05T10:00:00Z', updatedAt: '2026-11-05T10:00:00Z', prs: [],
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: {
    repoRoot: '', adoptionEnabled: false,
    engines: [{ engine: 'claude_code', available: true, unavailableReasons: {}, capabilities: CAPS }],
  } }))
  await page.route('**/api/models**', (route) => route.fulfill({ json: [
    { id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' },
  ] }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/api/sessions/*/diff**', (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route(`**/api/sessions/${sid}/worktree/preflight`, (route) => route.fulfill({ json: preflight }))
  for (const leaf of ['model', 'worktree/discard', 'worktree/repair']) {
    await page.route(`**/api/sessions/${sid}/${leaf}`, (route) => {
      posts.push(route.request())
      const json = leaf === 'model' ? { ok: true }
        : leaf === 'worktree/discard' ? DISCARDED
          : { repaired: true, path: CWD, branch: 'viberpit/parity-session', recoveryRefs: [] }
      return route.fulfill({ json })
    })
  }
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string; sessionId?: string }
    if (frame.type !== 'attach' || frame.sessionId !== sid) return
    ws.send(wire(replayFrame(sid, history, { totalTurns: 2 })))
    ws.send(JSON.stringify({ type: 'commands', sessionId: sid, commands: [
      { name: 'compact', description: 'Summarize the conversation', argumentHint: null, kind: 'builtin', source: 'cli' },
    ] }))
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session parity me/i }).click()
  await expect(page.getByText('Looking into it')).toBeVisible()
  return { posts }
}

/** The rail's Environment card: a column at 1440, the Details tab at 390. */
async function environment(page: Page, width: number) {
  if (width < 1024) await page.getByRole('tab', { name: 'Details' }).click()
  return page.getByRole('region', { name: 'Environment' })
}

async function noOverflow(page: Page) {
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
}

for (const width of [1440, 390]) {
  test(`${width}px: a rate-limited turn offers Switch model, Lower effort and Rewind & retry`, async ({ page }) => {
    const { posts } = await openSession(page, width)
    const card = page.getByTestId('transcript-scroll').getByRole('alert').filter({ hasText: 'Rate limited' })
    await expect(card.getByRole('button', { name: 'Switch model' })).toBeVisible()
    await expect(card.getByRole('button')).toHaveText(['Switch model', 'Lower effort', 'Rewind & retry'])
    await noOverflow(page)

    await card.getByRole('button', { name: 'Lower effort' }).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].postDataJSON()).toEqual({ model: 'opus', effort: 'medium', when: 'now' })

    // Switch model opens the prompt row's picker; it picks nothing by itself.
    await card.getByRole('button', { name: 'Switch model' }).click()
    await expect(page.getByRole('menu', { name: 'Model' })).toBeVisible()
    expect(posts).toHaveLength(1)
  })

  test(`${width}px: at the lowest effort there is no Lower effort; a missing model offers no retry`, async ({ page }) => {
    await openSession(page, width, { effort: 'low', history: failed('model_unavailable', 'api_404', 'The selected model does not exist') })
    const card = page.getByTestId('transcript-scroll').getByRole('alert').filter({ hasText: 'does not exist' })
    await expect(card.getByRole('button')).toHaveText(['Switch model'])
  })

  test(`${width}px: Discard confirms with the facts, leaves the session, and keeps the refs in a toast`, async ({ page }) => {
    const { posts } = await openSession(page, width)
    const env = await environment(page, width)
    await env.getByRole('button', { name: 'Discard…' }).click()
    const dialog = page.getByRole('dialog', { name: 'Discard this worktree?' })
    await expect(dialog.getByText('3 uncommitted files')).toBeVisible()
    await expect(dialog.getByText('2 unpushed commits')).toBeVisible()
    await expect(dialog.getByText('Branch viberpit/parity-session will be deleted.')).toBeVisible()
    await expect(dialog.getByText(PREFLIGHT.restoreHint)).toBeVisible()
    await expect(dialog.getByText(IGNORED)).toBeVisible()
    const discard = dialog.getByRole('button', { name: 'Discard', exact: true })
    await expect(discard).toBeDisabled()
    await dialog.getByRole('checkbox', { name: 'End the session and delete its worktree' }).check()
    await expect(discard).toBeEnabled()
    await noOverflow(page)
    await page.screenshot({ path: `/tmp/d19-${width}.png` })

    await discard.click()
    await expect(dialog).toBeHidden()
    await expect.poll(() => posts.map((p) => [new URL(p.url()).pathname, p.postDataJSON()]))
      .toEqual([[`/api/sessions/${sid}/worktree/discard`, { confirm: true }]])
    await expect(page).not.toHaveURL(new RegExp(`/sessions/${sid}`))
    const toast = page.getByText('Discarded — worktree and branch viberpit/parity-session removed')
    await expect(toast).toBeVisible()
    await expect(page.getByRole('button', { name: DISCARDED.restoreCommand })).toBeVisible()
  })

  test(`${width}px: a missing worktree offers Repair, not Discard`, async ({ page }) => {
    const { posts } = await openSession(page, width, {
      preflight: { ...PREFLIGHT, present: false, state: 'missing', canDiscard: false, canRepair: true },
    })
    const env = await environment(page, width)
    await env.getByRole('button', { name: 'Repair' }).click()
    await expect(env.getByRole('button', { name: 'Discard…' })).toHaveCount(0)
    await expect.poll(() => posts.map((p) => new URL(p.url()).pathname)).toEqual([`/api/sessions/${sid}/worktree/repair`])
    await expect(page.getByText('Worktree recreated on viberpit/parity-session')).toBeVisible()
  })

  test(`${width}px: ⌘K starts a session on a branch, and a refused checkout says why`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const creates: unknown[] = []
    await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/sessions', (route) => {
      if (route.request().method() === 'POST') {
        creates.push(route.request().postDataJSON())
        return route.fulfill({ status: 409, json: { error: {
          code: 'branch_switch_failed', message: "branch 'feat/thrust' is already checked out in another worktree" } } })
      }
      return route.fulfill({ json: [{
        id: 'pal-a', name: 'Refactor the rocket', repo: 'acme/rocket', branch: 'feat/thrust', accountName: 'alice',
        engine: 'claude_code', status: 'idle', createdAt: '2026-11-01T09:00:00Z', updatedAt: '2026-11-02T10:00:00Z', prs: [],
      }] })
    })
    await page.route('**/api/github/repos', (route) => route.fulfill({ json: [{
      nameWithOwner: 'acme/rocket', name: 'rocket', owner: 'acme', isPrivate: true, url: '', defaultBranch: 'main',
      pushedAt: null, cloned: true,
    }] }))
    await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
    await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
    await page.routeWebSocket('**/ws', () => {})
    await page.goto('/sessions')
    await expect(page.getByRole('button', { name: /open session refactor the rocket/i })).toBeVisible()

    await page.keyboard.press('Control+KeyK')
    const palette = page.getByRole('dialog', { name: 'Command palette' })
    await expect(palette.getByRole('combobox')).toBeFocused()
    await page.keyboard.type('new session acme/rocket')
    // In the Branches section, below the session on that branch.
    await palette.getByRole('option', { name: /New session on acme\/rocket@feat\/thrust/ }).click()
    await expect(palette).toBeHidden()

    await expect(page.getByText('Branch: feat/thrust')).toBeVisible()
    await expect(page.getByLabel('Repository')).toHaveValue('acme/rocket')
    await page.getByLabel('New session prompt').fill('Ship the thrust fix')
    await page.keyboard.press('Enter')
    await expect.poll(() => creates.length).toBe(1)
    expect(creates[0]).toMatchObject({ repo: 'acme/rocket', branch: 'feat/thrust', initialPrompt: 'Ship the thrust fix' })
    await expect(page.getByRole('alert').filter({ hasText: 'already checked out' })).toBeVisible()
    await noOverflow(page)
  })
}
