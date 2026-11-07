/**
 * D-23 opt-in sandbox in a real browser, route-mocked at 1440 and 390:
 *  - the Environment card's Sandbox row: the state, the toggle with its respawn warning
 *    (both directions), the one-line note while on, a refusal shown inline, and the
 *    toggle disabled with the host's preflight reason;
 *  - a session held on a failed sandbox: the banner states the reason, Retry posts at
 *    once, "Turn off sandbox and continue" confirms first;
 *  - Settings › Sessions: "Sandbox new sessions" only where the server has the capability.
 *
 * Run: npx playwright test e2e/sandbox.spec.ts
 */
import { test, expect, type Page, type Request } from '@playwright/test'
import { replayFrame, userTurn, assistantTurn, wire } from './fixtures/wsFrames'

const sid = 'sandbox-session'
const U1 = '22222222-2222-4222-8222-222222222222'
const CWD = `/home/u/worktrees/${sid}`
const NOTE = 'Network: GitHub only. Writes: this worktree only. In full-auto there are no prompts — the sandbox only contains commands.'

const CAPS = { models: true, reasoningEffort: true, steer: true, interrupt: true, approvals: true,
  questions: true, adopt: true, selfModelSwitch: true, subagents: true, attachments: true,
  accountRotation: true, accountSwitchNewThread: false, fork: false, rewind: true, flagSettings: true, sandbox: true }

type Sandbox = { enabled: boolean; state: 'off' | 'pending' | 'active' | 'failed'; reason: string | null }

const facts = (over: Record<string, unknown> = {}) => ({
  sessionId: sid, host: 'dobby', capability: true, enabled: false, state: 'off', reason: null, held: false, busy: false,
  available: true, unavailableReason: null,
  preflight: { ok: true, reason: null, checks: [], cliVersion: '2.1.319' },
  canToggle: true, allowedDomains: ['github.com', '*.github.com', '*.githubusercontent.com'], ...over,
})

interface Opts {
  sandbox?: Sandbox
  held?: boolean
  facts?: Record<string, unknown>
  /** The POST's answer: a status and body. */
  post?: { status: number; json: unknown }
  /** What `?refresh=true` (Re-check host) answers over `facts`. */
  refreshed?: Record<string, unknown>
  /** `/api/config`'s `capabilities.sandbox` (false = the kill switch is off). */
  capability?: boolean
}

async function openSession(page: Page, width: number, opts: Opts = {}) {
  let sandbox: Sandbox = opts.sandbox ?? { enabled: false, state: 'off', reason: null }
  let held = opts.held ?? false
  const posts: Request[] = []
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Sandbox me', accountName: 'alice', engine: 'claude_code', model: 'opus',
    status: held ? 'needs_attention' : 'idle', pausedReason: held ? 'sandbox_failed' : null, sandbox,
    repo: 'acme/rocket', branch: 'viberpit/sandbox-session', cwd: CWD,
    createdAt: '2026-11-06T10:00:00Z', updatedAt: '2026-11-06T10:00:00Z', prs: [],
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: {
    repoRoot: '', adoptionEnabled: false,
    engines: [{ engine: 'claude_code', available: true, unavailableReasons: {}, capabilities: { ...CAPS, sandbox: opts.capability ?? true } }],
  } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/api/sessions/*/diff**', (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  const refreshes: string[] = []
  await page.route(new RegExp(`/api/sessions/${sid}/sandbox(\\?.*)?$`), (route) => {
    if (route.request().method() !== 'POST') {
      const refresh = new URL(route.request().url()).searchParams.get('refresh') === 'true'
      if (refresh) refreshes.push(route.request().url())
      return route.fulfill({ json: facts({ enabled: sandbox.enabled, state: sandbox.state, reason: sandbox.reason, held,
        ...opts.facts, ...(refresh ? opts.refreshed : {}) }) })
    }
    posts.push(route.request())
    if (opts.post) return route.fulfill(opts.post)
    const { enabled } = route.request().postDataJSON() as { enabled: boolean }
    sandbox = { enabled, state: enabled ? 'active' : 'off', reason: null }
    held = false
    return route.fulfill({ json: { sessionId: sid, ...sandbox, held: false, respawned: true } })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string; sessionId?: string }
    if (frame.type !== 'attach' || frame.sessionId !== sid) return
    ws.send(wire(replayFrame(sid, [...userTurn(U1, 'Fix the build'), ...assistantTurn('t1', 'Done with the build')], { totalTurns: 2 })))
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session sandbox me/i }).click()
  await expect(page.getByText('Done with the build')).toBeVisible()
  return { posts, refreshes }
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

const bodies = (posts: Request[]) => posts.map((p) => p.postDataJSON())

for (const width of [1440, 390]) {
  test(`${width}px: turning the sandbox on warns of the restart, posts, and shows the note`, async ({ page }) => {
    const { posts } = await openSession(page, width)
    const env = await environment(page, width)
    const toggle = env.getByRole('switch', { name: 'Sandbox' })
    await expect(toggle).toHaveAttribute('aria-checked', 'false')
    await expect(env.getByTestId('sandbox-state')).toHaveText('Off')
    await expect(env.getByText(NOTE)).toHaveCount(0)

    await toggle.click()
    const dialog = page.getByRole('dialog', { name: 'Turn the sandbox on?' })
    await expect(dialog).toContainText('restarts the session')
    await noOverflow(page)
    await dialog.getByRole('button', { name: 'Restart with sandbox' }).click()
    await expect(dialog).toBeHidden()
    await expect.poll(() => bodies(posts)).toEqual([{ enabled: true }])
    await expect(env.getByTestId('sandbox-state')).toHaveText('Active')
    await expect(env.getByText(NOTE)).toBeVisible()
    await expect(toggle).toHaveAttribute('aria-checked', 'true')
    await expect(toggle).toBeEnabled()
    await noOverflow(page)
    await page.screenshot({ path: `/tmp/d23-env-${width}.png` })

    // Off says it restarts too.
    await toggle.click()
    const off = page.getByRole('dialog', { name: 'Turn the sandbox off?' })
    await expect(off).toContainText('restarts the session without the sandbox')
    await off.getByRole('button', { name: 'Restart without sandbox' }).click()
    await expect.poll(() => bodies(posts)).toEqual([{ enabled: true }, { enabled: false }])
  })

  test(`${width}px: an unavailable host disables the toggle with its reason`, async ({ page }) => {
    await openSession(page, width, { facts: { available: false, canToggle: false, unavailableReason: 'socat is not installed' } })
    const env = await environment(page, width)
    await expect(env.getByRole('switch', { name: 'Sandbox' })).toBeDisabled()
    await expect(env.getByTestId('sandbox-disabled-reason')).toHaveText('socat is not installed')
    await noOverflow(page)
  })

  test(`${width}px: Re-check host re-runs the preflight and unlocks a fixed host`, async ({ page }) => {
    const { refreshes } = await openSession(page, width, {
      facts: { available: false, canToggle: false, unavailableReason: 'socat is not installed' },
      refreshed: { available: true, canToggle: true, unavailableReason: null },
    })
    const env = await environment(page, width)
    const toggle = env.getByRole('switch', { name: 'Sandbox' })
    await expect(toggle).toBeDisabled()
    await env.getByRole('button', { name: 'Re-check host' }).click()
    await expect(toggle).toBeEnabled()
    expect(refreshes).toHaveLength(1)
    await expect(env.getByRole('button', { name: 'Re-check host' })).toHaveCount(0)
  })

  test(`${width}px: kill switch off under a held session — Turn off only, one shared confirm`, async ({ page }) => {
    const { posts } = await openSession(page, width, {
      held: true, sandbox: { enabled: true, state: 'failed', reason: 'the sandbox is turned off on this host' },
      capability: false,
      facts: { capability: false, available: false, preflight: null, canToggle: true },
    })
    // The banner: no Retry (the server would refuse it), Turn off stays.
    const hold = page.getByTestId('sandbox-hold')
    await expect(hold.getByRole('button', { name: 'Turn off sandbox and continue' })).toBeVisible()
    await expect(hold.getByRole('button', { name: 'Retry' })).toHaveCount(0)
    // The card keeps the enabled row, and turning it off asks the same question.
    const env = await environment(page, width)
    const toggle = env.getByRole('switch', { name: 'Sandbox' })
    await expect(toggle).toHaveAttribute('aria-checked', 'true')
    await toggle.click()
    const dialog = page.getByRole('dialog', { name: 'Continue without the sandbox?' })
    await dialog.getByRole('button', { name: 'Turn off and continue' }).click()
    await expect.poll(() => bodies(posts)).toEqual([{ enabled: false }])
    await noOverflow(page)
  })

  test(`${width}px: a refused toggle is shown inline`, async ({ page }) => {
    await openSession(page, width, { post: { status: 409, json: { detail: {
      code: 'SANDBOX_UNAVAILABLE', message: 'unavailable', reason: 'bubblewrap (bwrap) is not installed' } } } })
    const env = await environment(page, width)
    await env.getByRole('switch', { name: 'Sandbox' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Restart with sandbox' }).click()
    await expect(env.getByRole('alert')).toHaveText('The sandbox is unavailable on this host: bubblewrap (bwrap) is not installed')
  })

  test(`${width}px: a held session offers Retry and a confirmed Turn off`, async ({ page }) => {
    const { posts } = await openSession(page, width, {
      held: true, sandbox: { enabled: true, state: 'failed', reason: 'the sandbox did not start' },
    })
    const hold = page.getByTestId('sandbox-hold')
    await expect(hold).toContainText('Sandbox failed — session held: the sandbox did not start')
    await expect(hold.getByRole('button', { name: 'Retry' })).toBeVisible()
    await noOverflow(page)
    await page.screenshot({ path: `/tmp/d23-hold-${width}.png` })

    await hold.getByRole('button', { name: 'Turn off sandbox and continue' }).click()
    const dialog = page.getByRole('dialog', { name: 'Continue without the sandbox?' })
    await expect(dialog).toContainText('no longer contained')
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    expect(posts).toHaveLength(0)

    await hold.getByRole('button', { name: 'Retry' }).click()
    await expect.poll(() => bodies(posts)).toEqual([{ enabled: true }])
    // The respawn verified: the next list read drops the hold.
    await expect(hold).toBeHidden()
  })

  test(`${width}px: Turn off sandbox and continue posts enabled: false after the confirm`, async ({ page }) => {
    const { posts } = await openSession(page, width, {
      held: true, sandbox: { enabled: true, state: 'failed', reason: 'the sandbox did not start' },
    })
    const hold = page.getByTestId('sandbox-hold')
    await hold.getByRole('button', { name: 'Turn off sandbox and continue' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Turn off and continue' }).click()
    await expect.poll(() => bodies(posts)).toEqual([{ enabled: false }])
    await expect(hold).toBeHidden()
  })

  test(`${width}px: a new session the host can't sandbox says the default is on, with the reason`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const creates: unknown[] = []
    await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/sessions', (route) => {
      if (route.request().method() === 'POST') {
        creates.push(route.request().postDataJSON())
        return route.fulfill({ status: 409, json: { error: {
          code: 'SANDBOX_UNAVAILABLE', message: 'unavailable', reason: 'socat is not installed' } } })
      }
      return route.fulfill({ json: [] })
    })
    await page.route('**/api/github/repos', (route) => route.fulfill({ json: [{
      nameWithOwner: 'acme/rocket', name: 'rocket', owner: 'acme', isPrivate: true, url: '', defaultBranch: 'main',
      pushedAt: null, cloned: true,
    }] }))
    await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
    await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
    await page.routeWebSocket('**/ws', () => {})
    await page.goto('/sessions')
    await page.getByLabel('Repository').selectOption('acme/rocket')
    await page.getByLabel('New session prompt').fill('Ship it')
    await page.keyboard.press('Enter')
    await expect.poll(() => creates.length).toBe(1)
    const alert = page.getByRole('alert').filter({ hasText: 'Sandbox new sessions' })
    await expect(alert).toContainText('"Sandbox new sessions" is on and the sandbox can\'t run on this host (socat is not installed).')
    await noOverflow(page)
  })

  test(`${width}px: Settings › Sessions offers "Sandbox new sessions" only with the capability`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const puts: Record<string, unknown>[] = []
    let capability = false
    await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/settings', (route) => {
      if (route.request().method() === 'PUT') puts.push(route.request().postDataJSON() as Record<string, unknown>)
      return route.fulfill({ json: {
        defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null, defaultToolDetail: 'auto',
        autoArchiveEnabled: true, maxArchivedSessions: null, archiveHome: null, maxActiveSessions: null,
        modelApprovalAction: null, modelApprovalDenylist: null, branchPrefix: null,
        sandboxDefault: puts.at(-1)?.sandboxDefault === true, sandboxCapability: capability,
        acceptedPermissionModes: ['interactive'],
        effectiveDefaults: {
          repoRoot: '/home/user/GitHub', archiveHome: null, modelApprovalAction: 'require_approval',
          modelApprovalDenylist: 'fable', globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] },
          branchPrefix: 'viberpit/',
        },
        managed: [], about: { gitSha: null, hostName: 'edge' },
      } })
    })
    await page.goto('/settings/sessions')
    await expect(page.getByRole('textbox', { name: 'Branch prefix' })).toBeVisible()
    await expect(page.getByRole('checkbox', { name: 'Sandbox new sessions' })).toHaveCount(0)

    capability = true
    await page.reload()
    const box = page.getByRole('checkbox', { name: 'Sandbox new sessions' })
    await expect(box).not.toBeChecked()
    await expect(page.getByText(/Off by default.*bwrap.*socat/)).toBeVisible()
    await box.check()
    await page.getByRole('button', { name: 'Save' }).click()
    await expect.poll(() => puts.at(-1)?.sandboxDefault).toBe(true)
    await noOverflow(page)
  })
}

test.describe('touch targets — phone, coarse pointer', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('the hold actions are at least 44px tall', async ({ page }) => {
    await openSession(page, 390, { held: true, sandbox: { enabled: true, state: 'failed', reason: 'x' } })
    const hold = page.getByTestId('sandbox-hold')
    for (const name of ['Retry', 'Turn off sandbox and continue']) {
      const box = await hold.getByRole('button', { name }).boundingBox()
      expect(box!.height, name).toBeGreaterThanOrEqual(44)
    }
  })
})
