/**
 * MCP per-tool switches (D-16b). The session Context card: a connected server expands to its
 * tools (read from the session MCP GET — the live `mcpStatus` event carries none), a toggle
 * posts the session override and shows the "Session" chip from the reply, and a refusal leaves
 * the switch where it was with the server's text. Settings › MCP: the last-seen list with
 * "Seen …", a toggle PUTs the default, and an unseen server shows the Test hint. At 1440 (rail
 * column) and 390 (Details tab). The API is mocked with `page.route`, like the other session specs.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'session-mcp-tools'
const ANN = { readOnly: null, destructive: null, openWorld: null }
const LONG = Array(12).fill('Drops every table in the database and cannot be undone.').join(' ')

interface Tool {
  name: string
  description: string | null
  annotations: typeof ANN | Record<string, boolean | null>
  enabled: boolean
  source: 'default' | 'session'
}

function sessionTools(): Tool[] {
  return [
    { name: 'list_issues', description: 'Lists the issues of a repository.', annotations: { ...ANN, readOnly: true }, enabled: true, source: 'default' },
    { name: 'drop_database', description: LONG, annotations: { ...ANN, destructive: true }, enabled: true, source: 'default' },
    { name: 'flaky_tool', description: null, annotations: ANN, enabled: true, source: 'default' },
  ]
}

async function mockApi(page: Page) {
  const tools = sessionTools()
  const posts: { path: string; body: unknown }[] = []
  const puts: { path: string; body: unknown }[] = []
  const settingsTools: Tool[] = sessionTools().map((t) => ({ ...t }))
  const server = (extra: Record<string, unknown> = {}) => ({
    name: 'github', status: 'connected', error: null, toolCount: tools.length, scope: 'user', auth: null, ...extra,
  })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'MCP tools', accountName: 'alice', engine: 'claude_code', status: 'idle',
    branch: 'feat/tools', cwd: '/home/u/worktrees/tools',
    createdAt: '2026-11-05T10:00:00Z', updatedAt: '2026-11-05T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route(`**/api/sessions/${sid}/mcp`, (route) =>
    route.fulfill({ json: { servers: [server({ tools, toolsEnforced: true }), server({ name: 'sentry', tools: null })] } }))
  await page.route(`**/api/sessions/${sid}/mcp/*/tools/*`, (route) => {
    const url = new URL(route.request().url())
    const body = route.request().postDataJSON() as { enabled: boolean }
    posts.push({ path: url.pathname, body })
    const name = decodeURIComponent(url.pathname.split('/').pop()!)
    if (name === 'flaky_tool') {
      return route.fulfill({ status: 409, json: { detail: { code: 'SETTINGS_FAILED', message: 'The session did not confirm the change in time.' } } })
    }
    const t = tools.find((x) => x.name === name)!
    t.enabled = body.enabled
    t.source = 'session'
    return route.fulfill({ json: { servers: [server({ tools, toolsEnforced: true }), server({ name: 'sentry', tools: null })] } })
  })
  await page.route('**/api/library/mcp', (route) => route.fulfill({ json: { servers: ['github', 'sentry'].map((name) => ({
    name, source: 'user', readonly: false, transport: 'stdio', config: { type: 'stdio', command: name },
    overall: { status: 'connected', error: null, grant: 'none' }, status: [],
  })) } }))
  await page.route('**/api/library/mcp/scope', (route) => route.fulfill({ json: {
    servers: {
      github: { defaultEnabled: true, tools: settingsTools, toolsSeenAt: new Date(Date.now() - 5 * 60_000).toISOString() },
      sentry: { defaultEnabled: true, tools: null, toolsSeenAt: null },
    },
    rules: [],
  } }))
  await page.route('**/api/library/mcp/*/tools/*', (route) => {
    const url = new URL(route.request().url())
    const body = route.request().postDataJSON() as { enabled: boolean }
    puts.push({ path: url.pathname, body })
    settingsTools.find((t) => t.name === decodeURIComponent(url.pathname.split('/').pop()!))!.enabled = body.enabled
    return route.fulfill({ status: 204, body: '' })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string }
    if (frame.type !== 'attach') return
    // The live status: no `tools`, as the server sends it.
    ws.send(wire(replayFrame(sid, [{ type: 'mcpStatus', servers: [server(), server({ name: 'sentry' })] }], { totalTurns: 0 })))
  }))
  return { posts, puts }
}

async function noOverflow(page: Page) {
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
}

for (const width of [1440, 390]) {
  test(`${width}px: session tool switches and Settings tool defaults`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const { posts, puts } = await mockApi(page)
    await page.goto('/')
    await page.getByRole('button', { name: /open session mcp tools/i }).click()

    let card
    if (width === 1440) {
      card = page.getByRole('complementary', { name: 'Session details' }).getByRole('region', { name: 'Context' })
    } else {
      await page.getByRole('tab', { name: 'Details' }).click()
      card = page.locator('#tab-panel-details').getByRole('region', { name: 'Context' })
    }
    await card.getByRole('button', { name: 'Tools for github' }).click()
    const list = card.getByRole('list', { name: 'github tools' })
    await expect(list.getByRole('listitem')).toHaveCount(3)
    const rows = list.getByRole('listitem')
    await expect(rows.nth(0)).toContainText('read-only')
    await expect(rows.nth(1)).toContainText('destructive')
    await expect(rows.nth(1).getByTitle(LONG)).toBeVisible()
    await noOverflow(page)

    // Toggle: the route gets the tool, the reply's override shows the Session chip.
    const drop = card.getByRole('switch', { name: 'drop_database enabled' })
    await expect(drop).toHaveAttribute('aria-checked', 'true')
    await drop.click()
    await expect(drop).toHaveAttribute('aria-checked', 'false')
    await expect(rows.nth(1)).toContainText('Session')
    expect(posts).toEqual([{ path: `/api/sessions/${sid}/mcp/github/tools/drop_database`, body: { enabled: false } }])

    // A refusal leaves the switch where it was and shows the server's text.
    const flaky = card.getByRole('switch', { name: 'flaky_tool enabled' })
    await flaky.click()
    await expect(card.getByRole('alert')).toHaveText('The session did not confirm the change in time.')
    await expect(flaky).toHaveAttribute('aria-checked', 'true')
    // Inline only — no second copy as a toast.
    await expect(page.getByText('The session did not confirm the change in time.')).toHaveCount(1)

    // A server without a tool list has no switches.
    await card.getByRole('button', { name: 'Tools for sentry' }).click()
    await expect(card.getByText('Tool list unavailable')).toBeVisible()
    await card.getByRole('button', { name: 'Tools for github' }).click()
    await expect(list).toBeVisible()
    await noOverflow(page)
    await list.scrollIntoViewIfNeeded()
    await page.screenshot({ path: `/tmp/d16b-${width}.png`, fullPage: false })

    // Settings › MCP: the last-seen list, and the hint for an unseen server.
    await page.goto('/settings/mcp')
    await expect(page.getByText('Test the server to load its tools.')).toBeVisible()
    await page.getByRole('button', { name: 'Tools for github' }).click()
    const panel = page.getByRole('group', { name: 'Tool defaults for github' })
    await expect(panel).toContainText('Defaults for new and restarted Claude sessions · Seen 5m ago')
    await panel.getByRole('switch', { name: 'list_issues enabled' }).click()
    await expect(panel.getByRole('switch', { name: 'list_issues enabled' })).toHaveAttribute('aria-checked', 'false')
    expect(puts).toEqual([{ path: '/api/library/mcp/github/tools/list_issues', body: { enabled: false } }])
    await expect(panel).not.toContainText('Session')
    await noOverflow(page)
    await page.screenshot({ path: `/tmp/d16b-${width}-settings.png`, fullPage: false })
  })
}
