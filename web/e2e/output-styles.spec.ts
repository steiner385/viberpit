/**
 * D-16a output styles in a real browser, at desktop and phone width: the session
 * composer's + menu Style › submenu (the session's own list, current checked, a pick
 * POSTs …/output-style, hidden for Codex); the server's refusal notices for app
 * commands render in the transcript, and a refused command keeps its text in the box;
 * Settings › Output styles saves the default and opens a user style read-only.
 * Route-mocked: no backend.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'styles-d16a'
const COPY = 'Changing style mid-session re-sends the system prompt on the next turn.'
const STYLES = ['default', 'Proactive', 'Concise', 'Explanatory', 'Learning', 'plugin:Terse']
const PLAN = { name: 'plan', description: 'Turn plan mode on or off (bare: toggle)', argumentHint: '[on|off]', kind: 'control', source: 'app' }
const OUTPUT_STYLE = { name: 'output-style', description: "Set this session's output style", argumentHint: '[style]', kind: 'control', source: 'app' }
const AUTOMATION = 'Slash commands from automation are not run: /plan'
const MORE_TEXT = "/plan wasn't run and nothing was sent: the text after it can't go with a slash command. Send the command on its own."

async function mockApp(page: Page, width: number, engine: 'claude_code' | 'codex' = 'claude_code') {
  await page.setViewportSize({ width, height: 900 })
  const posts: { url: string; body: unknown }[] = []
  let outputStyle = 'Concise'
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Styles D16a', accountName: 'alice', engine, status: 'idle',
    branch: 'viberpit/x', cwd: '/home/u/wt/x', model: 'opus',
    outputStyle: engine === 'codex' ? null : outputStyle,
    availableOutputStyles: engine === 'codex' ? [] : STYLES,
    createdAt: '2026-11-04T10:00:00Z', updatedAt: '2026-11-04T10:00:00Z',
  }] }))
  await page.route(`**/api/sessions/${sid}/output-style`, (route) => {
    const { style } = route.request().postDataJSON() as { style: string }
    outputStyle = style
    posts.push({ url: 'output-style', body: { style } })
    return route.fulfill({ json: { style } })
  })
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'none' } }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string; name?: string; text?: string }
    if (frame.type === 'attach') {
      ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
      ws.send(JSON.stringify({ type: 'commands', sessionId: sid, commands: engine === 'codex' ? [PLAN] : [PLAN, OUTPUT_STYLE] }))
    }
    // The browser on the edge host is not an attested person: the command frame is refused.
    if (frame.type === 'command' && frame.name === 'plan') {
      ws.send(JSON.stringify({ type: 'commandResult', sessionId: sid, name: 'plan', ok: false, render: 'notice',
        error: 'app command refused', data: { text: AUTOMATION } }))
    }
    // An app command with more text after it is refused whole and acked `failed`.
    if (frame.type === 'prompt' && frame.text?.startsWith('/plan')) {
      ws.send(JSON.stringify({ type: 'promptAck', sessionId: sid, stage: 'received' }))
      ws.send(JSON.stringify({ type: 'commandResult', sessionId: sid, name: 'plan', ok: false, render: 'notice',
        error: 'app command refused', data: { text: MORE_TEXT } }))
      ws.send(JSON.stringify({ type: 'promptAck', sessionId: sid, stage: 'failed' }))
    }
  }))
  return posts
}

async function noSidewaysScroll(page: Page) {
  const doc = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }))
  expect(doc.sw).toBeLessThanOrEqual(doc.cw)
}

async function openSession(page: Page) {
  await page.goto('/sessions')
  await page.getByRole('button', { name: /open session styles d16a/i }).click()
  await expect(page.getByRole('textbox', { name: 'Message' })).toBeVisible()
}

for (const width of [1440, 390]) {
  test.describe(`${width}px`, () => {
    test('the + menu Style › lists the session styles, current checked, and a pick posts', async ({ page }) => {
      const posts = await mockApp(page, width)
      await openSession(page)
      await page.getByRole('button', { name: 'Add' }).click()
      const menu = page.getByRole('menu', { name: 'Add' })
      await expect(menu.getByRole('menuitem')).toHaveText([/Attach file/, /Slash commands/, /Style ›/])
      const style = menu.getByRole('menuitem', { name: /Style ›/ })
      await expect(style).toHaveAttribute('title', COPY)
      await style.click()
      const sub = page.getByRole('menu', { name: 'Style' })
      await expect(sub.getByRole('menuitemradio')).toHaveText(STYLES.map((s) => new RegExp(`^${s}`)))
      await expect(sub.getByRole('menuitemradio', { name: /Concise/ })).toHaveAttribute('aria-checked', 'true')
      await noSidewaysScroll(page)
      await sub.getByRole('menuitemradio', { name: /Learning/ }).click()
      await expect.poll(() => posts).toEqual([{ url: 'output-style', body: { style: 'Learning' } }])
      await page.getByRole('button', { name: 'Add' }).click()
      await page.getByRole('menu', { name: 'Add' }).getByRole('menuitem', { name: /Style ›/ }).click()
      await expect(page.getByRole('menu', { name: 'Style' }).getByRole('menuitemradio', { name: /Learning/ }))
        .toHaveAttribute('aria-checked', 'true')
    })

    test("a refused /plan shows the server's notice; with more text it keeps the box", async ({ page }) => {
      await mockApp(page, width)
      await openSession(page)
      const box = page.getByRole('textbox', { name: 'Message' })
      await box.fill('/plan on')
      await box.press('Enter')
      await expect(page.getByText(AUTOMATION)).toBeVisible()

      await box.fill('/plan on\nand then refactor it')
      await box.press('Enter')
      await expect(page.getByText(MORE_TEXT)).toBeVisible()
      await expect(box).toHaveValue('/plan on\nand then refactor it')
      await noSidewaysScroll(page)
    })

    test('a Codex session has no Style › and no /output-style; /plan is listed', async ({ page }) => {
      await mockApp(page, width, 'codex')
      await openSession(page)
      await page.getByRole('button', { name: 'Add' }).click()
      const menu = page.getByRole('menu', { name: 'Add' })
      await expect(menu.getByRole('menuitem')).toHaveText([/Attach file/, /Slash commands/])
      await page.keyboard.press('Escape')
      const box = page.getByRole('textbox', { name: 'Message' })
      await box.fill('/')
      const palette = page.getByRole('listbox')
      await expect(palette).toContainText('plan')
      await expect(palette).not.toContainText('output-style')
    })

    test('Settings › Output styles saves the default and opens a user style read-only', async ({ page }) => {
      await mockApp(page, width)
      const puts: unknown[] = []
      let saved: string | null = null
      await page.route('**/api/settings', (route) => {
        if (route.request().method() === 'PUT') {
          const body = route.request().postDataJSON() as { defaultOutputStyle: string | null }
          puts.push(body)
          saved = body.defaultOutputStyle
        }
        return route.fulfill({ json: {
          defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null, defaultToolDetail: 'auto',
          autoArchiveEnabled: true, maxArchivedSessions: null, archiveHome: null, modelApprovalAction: null,
          modelApprovalDenylist: null, acceptedPermissionModes: ['interactive'], defaultOutputStyle: saved,
          effectiveDefaults: { repoRoot: '/home/user/GitHub', archiveHome: null, modelApprovalAction: 'require_approval',
            modelApprovalDenylist: 'fable', globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] } },
          managed: [], about: { gitSha: null, hostName: 'dobby' },
        } })
      })
      await page.route('**/api/settings/output-styles', (route) => route.fulfill({ json: { styles: [
        ...['default', 'Proactive', 'Concise', 'Explanatory', 'Learning'].map((name) => ({ name, source: 'builtin', description: null })),
        { name: 'Terse', source: 'user', description: 'Short answers, no preamble.', path: 'terse.md' },
      ] } }))
      await page.route('**/api/settings/output-styles/terse.md', (route) => route.fulfill({ json: {
        name: 'Terse', path: 'terse.md', content: '---\nname: Terse\ndescription: Short answers, no preamble.\n---\nAnswer in as few words as will do.\n',
      } }))
      await page.goto('/settings/output-styles')
      const picker = page.getByRole('combobox', { name: 'Default style' })
      await expect(picker).toHaveValue('default')
      await expect(page.getByRole('listitem').filter({ hasText: 'Terse' })).toContainText('User')
      await expect(page.getByRole('listitem').filter({ hasText: 'Learning' })).toContainText('Built-in')
      await noSidewaysScroll(page)

      await picker.selectOption('Terse')
      await page.getByRole('button', { name: 'Save' }).click()
      await expect.poll(() => puts).toEqual([{ defaultOutputStyle: 'Terse' }])
      await expect(page.getByRole('button', { name: 'Save' })).toBeDisabled()

      await page.getByRole('button', { name: 'View Terse' }).click()
      const dialog = page.getByRole('dialog', { name: 'Terse' })
      await expect(dialog).toContainText('Answer in as few words as will do.')
      await expect(dialog.getByRole('textbox')).toHaveCount(0)
      await noSidewaysScroll(page)
      await dialog.getByRole('button', { name: 'Close' }).click()
      await expect(dialog).toHaveCount(0)
    })
  })
}
