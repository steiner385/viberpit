/**
 * D-14 session settings in a real browser, at desktop and phone width: the dispatch
 * bar's + menu (Attach image, Presets ›, Templates ›) and its preset chip reaching
 * POST /sessions as explicit fields (never `presetId`); ⌘K's "New session from preset"; the prompt row's ⚡
 * (POST …/fast) with its once-per-browser cost note; the composer's + menu; the
 * Environment card's PR auto-fix switch; Settings › Sessions' worktree location.
 * Route-mocked: no backend.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'settings-d14'
const FAST_COPY = 'Fast mode — faster output on the same model. Bills usage credits at a higher rate, outside your plan.'

const PRESET = {
  id: 'p1', name: 'Bug bash', repo: null, account: null, model: null, permissionMode: 'plan',
  initialPrompt: 'Triage the open bugs', branchStrategy: null, permissionProfileId: null, createdAt: null, updatedAt: null,
}
const TEMPLATE = {
  id: 't1', name: 'Review', content: 'Review {{area}}', variables: [{ name: 'area', default: 'auth' }],
  tags: [], createdAt: null, updatedAt: null,
}

async function mockApp(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 })
  const posts: { url: string; body: unknown }[] = []
  let fastMode = false
  let paused = false
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => {
    if (route.request().method() === 'POST') {
      posts.push({ url: '/api/sessions', body: route.request().postDataJSON() })
      return route.fulfill({ status: 500, json: { detail: { message: 'stop here' } } })
    }
    return route.fulfill({ json: [{
      id: sid, name: 'Settings D14', accountName: 'alice', engine: 'claude_code', status: 'idle',
      branch: 'viberpit/x', cwd: '/home/u/wt/x', model: 'opus', fastMode, fastModeAvailable: true,
      prAutofixPaused: paused,
      prs: [{ number: 7, url: 'https://github.com/o/r/pull/7', state: 'open', checks: 'failing', title: 'PR 7', deployed: false }],
      createdAt: '2026-11-04T10:00:00Z', updatedAt: '2026-11-04T10:00:00Z',
    }] })
  })
  await page.route('**/api/presets', (route) => route.fulfill({ json: [PRESET] }))
  await page.route('**/api/templates', (route) => route.fulfill({ json: [TEMPLATE] }))
  await page.route('**/api/templates/t1/render', (route) => {
    posts.push({ url: '/api/templates/t1/render', body: route.request().postDataJSON() })
    return route.fulfill({ json: { rendered: 'Review auth' } })
  })
  await page.route(`**/api/sessions/${sid}/fast`, (route) => {
    const { enabled } = route.request().postDataJSON() as { enabled: boolean }
    fastMode = enabled
    posts.push({ url: 'fast', body: { enabled } })
    return route.fulfill({ json: { enabled } })
  })
  await page.route(`**/api/sessions/${sid}/pr-autofix`, (route) => {
    const { enabled } = route.request().postDataJSON() as { enabled: boolean }
    paused = !enabled
    posts.push({ url: 'pr-autofix', body: { enabled } })
    return route.fulfill({ json: { enabled } })
  })
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'none' } }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    if ((JSON.parse(msg.toString()) as { type: string }).type === 'attach') ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
  }))
  return posts
}

async function noSidewaysScroll(page: Page) {
  const doc = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }))
  expect(doc.sw).toBeLessThanOrEqual(doc.cw)
}

for (const width of [1440, 390]) {
  test.describe(`${width}px`, () => {
    test('the dispatch + menu: a preset chip rides on the create; a template renders in', async ({ page }) => {
      const posts = await mockApp(page, width)
      await page.goto('/sessions')
      const prompt = page.getByRole('textbox', { name: 'New session prompt' })
      await expect(prompt).toBeVisible()
      await expect(page.getByRole('button', { name: 'Attach image' })).toHaveCount(0)

      await page.getByRole('button', { name: 'Add' }).click()
      const menu = page.getByRole('menu', { name: 'Add' })
      await expect(menu.getByRole('menuitem')).toHaveText([/Attach image/, /Presets ›/, /Templates ›/])
      await menu.getByRole('menuitem', { name: /Presets/ }).click()
      await page.getByRole('menu', { name: 'Presets' }).getByRole('menuitem', { name: /Bug bash/ }).click()
      await expect(page.getByText('Preset: Bug bash')).toBeVisible()
      await expect(prompt).toHaveValue('Triage the open bugs')
      await noSidewaysScroll(page)

      await page.getByRole('button', { name: 'Create session' }).click()
      await expect.poll(() => posts.find((p) => p.url === '/api/sessions')?.body)
        .toMatchObject({ permissionMode: 'plan', initialPrompt: 'Triage the open bugs' })
      // The bar sends what it shows, never the preset id (D-14 final review).
      expect(posts.find((p) => p.url === '/api/sessions')?.body).not.toHaveProperty('presetId')

      await prompt.fill('')
      await page.getByRole('button', { name: 'Add' }).click()
      await page.getByRole('menu', { name: 'Add' }).getByRole('menuitem', { name: /Templates/ }).click()
      await page.getByRole('menu', { name: 'Templates' }).getByRole('menuitem', { name: /Review/ }).click()
      await expect(prompt).toHaveValue('Review auth')
      expect(posts).toContainEqual({ url: '/api/templates/t1/render', body: { variables: { area: 'auth' } } })
    })

    test('⌘K lists "New session from preset" and lands on the bar with the chip set', async ({ page }) => {
      await mockApp(page, width)
      await page.goto('/sessions')
      await expect(page.getByRole('textbox', { name: 'New session prompt' })).toBeVisible()
      await page.keyboard.press('Control+KeyK')
      const palette = page.getByRole('dialog', { name: 'Command palette' })
      await page.keyboard.type('bug bash')
      // Sessions rank above Actions; pick the action itself.
      await palette.getByRole('option', { name: /New session from preset: Bug bash/ }).click()
      await expect(page.getByText('Preset: Bug bash')).toBeVisible()
    })

    test('⚡ posts /fast and shows the cost note once; the composer + menu offers Attach', async ({ page }) => {
      const posts = await mockApp(page, width)
      await page.goto('/sessions')
      await page.getByRole('button', { name: /open session settings d14/i }).click()
      const bolt = page.getByRole('button', { name: 'Fast mode', exact: true })
      await expect(bolt).toHaveAttribute('title', FAST_COPY)
      await expect(bolt).toHaveAttribute('aria-pressed', 'false')
      await bolt.click()
      await expect.poll(() => posts.filter((p) => p.url === 'fast')).toEqual([{ url: 'fast', body: { enabled: true } }])
      await expect(page.getByRole('note')).toHaveText(FAST_COPY)
      await expect(bolt).toHaveAttribute('aria-pressed', 'true')
      const row = await page.getByTestId('prompt-row').evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth }))
      expect(row.sw).toBeLessThanOrEqual(row.cw)

      // Off and on again: the note has been seen in this browser.
      await page.getByRole('button', { name: 'Dismiss fast mode note' }).click()
      await bolt.click()
      await expect(bolt).toHaveAttribute('aria-pressed', 'false')
      await bolt.click()
      await expect(bolt).toHaveAttribute('aria-pressed', 'true')
      await expect(page.getByRole('note')).toHaveCount(0)

      await page.getByRole('button', { name: 'Add' }).click()
      await expect(page.getByRole('menu', { name: 'Add' }).getByRole('menuitem')).toHaveText([/Attach file/])
      await noSidewaysScroll(page)
    })
  })
}

test('1440px: the Environment card PR auto-fix switch posts pr-autofix', async ({ page }) => {
  const posts: { url: string; body: unknown }[] = await mockApp(page, 1440)
  await page.goto('/sessions')
  await page.getByRole('button', { name: /open session settings d14/i }).click()
  const env = page.getByRole('complementary', { name: 'Session details' }).getByRole('region', { name: 'Environment' })
  const toggle = env.getByRole('switch', { name: 'PR auto-fix' })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await expect(env.getByText("PR Doctor fixes failing checks and conflicts on this session's PRs")).toBeVisible()
  await toggle.click()
  await expect.poll(() => posts.filter((p) => p.url === 'pr-autofix')).toEqual([{ url: 'pr-autofix', body: { enabled: false } }])
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
})

for (const width of [1440, 390]) {
  test(`${width}px: Settings › Sessions shows the worktree location and the branch prefix field`, async ({ page }) => {
    await mockApp(page, width)
    await page.route('**/api/settings', (route) => route.fulfill({ json: {
      defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null, defaultToolDetail: 'auto',
      autoArchiveEnabled: true, maxArchivedSessions: null, archiveHome: null, modelApprovalAction: null,
      modelApprovalDenylist: null, acceptedPermissionModes: ['interactive'],
      effectiveDefaults: { repoRoot: '/home/user/GitHub', archiveHome: null, modelApprovalAction: 'require_approval',
        modelApprovalDenylist: 'fable', globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] },
        branchPrefix: 'viberpit/' },
    } }))
    await page.goto('/settings/sessions')
    const root = page.getByLabel('Worktree location', { exact: true })
    await expect(root).toBeVisible()
    await expect(root).toHaveAttribute('placeholder', '/home/user/GitHub · server default')
    await expect(page.getByText(/Changing it doesn't move existing worktrees/)).toBeVisible()
    // D-22: the prefix is a setting now (its own flows: e2e/branch-prefix.spec.ts).
    await expect(page.getByRole('textbox', { name: 'Branch prefix' })).toHaveAttribute('placeholder', 'viberpit/')
    await noSidewaysScroll(page)
  })
}
