/**
 * Memory files (D-13): the Context card lists the memory files the CLI loaded
 * (from the `contextUsage` frame), a row opens a read-only viewer, the user file
 * links to Settings › Memory, and the editor there survives a conflicting save
 * without losing the draft. At 1440 (rail column) and 390 (Details tab). The API
 * is mocked with `page.route`, like the other session specs.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'session-memory'
const USER = '/home/u/.claude-accounts/cfg/alice/CLAUDE.md'
const PROJECT = '/home/u/worktrees/mem/CLAUDE.md'
const PROPAGATION = 'Sessions on other hosts can take up to 10 minutes to see this change'
const COPY = 'Applies to new sessions. Running sessions keep the instructions they started with.'

async function mockApi(page: Page) {
  const puts: { text: string; etag: string }[] = []
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Memory files', accountName: 'alice', engine: 'claude_code', status: 'idle',
    branch: 'feat/mem', cwd: '/home/u/worktrees/mem',
    createdAt: '2026-11-03T10:00:00Z', updatedAt: '2026-11-03T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route(`**/api/sessions/${sid}/memory-file**`, (route) => {
    const path = new URL(route.request().url()).searchParams.get('path')
    if (path === USER) {
      return route.fulfill({ json: { path, type: 'User', text: '# Global\nAlways run the tests.\n', truncated: false, editable: true } })
    }
    if (path === PROJECT) {
      return route.fulfill({ json: { path, type: 'Project', text: '# Repo rules\nUse ruff.\n', truncated: false, editable: false } })
    }
    return route.fulfill({ status: 404, json: { detail: { code: 'MEMORY_FILE_UNKNOWN' } } })
  })
  await page.route('**/api/settings/memory', (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as { text: string; etag: string }
      puts.push(body)
      // The first save races an edit made elsewhere; the retry after Reload lands.
      if (body.etag === 'e1') {
        return route.fulfill({ status: 409, json: { detail: { code: 'MEMORY_CONFLICT', text: '# Global\nEdited on rocky.\n', etag: 'e2' } } })
      }
      return route.fulfill({ json: { etag: 'e3', propagation: PROPAGATION } })
    }
    return route.fulfill({ json: { text: '# Global\nAlways run the tests.\n', etag: 'e1', exists: true } })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string }
    if (frame.type !== 'attach') return
    ws.send(wire(replayFrame(sid, [{
      type: 'contextUsage', usedTokens: 40_000, maxTokens: 200_000, percentage: 20,
      memoryFiles: [
        { path: USER, type: 'User', tokens: 1234 },
        { path: PROJECT, type: 'Project', tokens: 958 },
      ],
    }], { totalTurns: 0 })))
  }))
  return puts
}

/** Neither the page nor (when open) the dialog scrolls sideways. */
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

for (const width of [1440, 390]) {
  test(`${width}px: the card lists the loaded files, opens one, and the user file edits through a conflict`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const puts = await mockApi(page)
    await page.goto('/')
    await page.getByRole('button', { name: /open session memory files/i }).click()

    // The rail is a column at 1440 and a Details tab at 390.
    let card
    if (width === 1440) {
      card = page.getByRole('complementary', { name: 'Session details' }).getByRole('region', { name: 'Context' })
    } else {
      await page.getByRole('tab', { name: 'Details' }).click()
      card = page.locator('#tab-panel-details').getByRole('region', { name: 'Context' })
    }
    const list = card.getByRole('list', { name: 'Memory files' })
    await expect(list).toBeVisible()
    const rows = list.getByRole('listitem')
    await expect(rows).toHaveCount(2)
    // CLI order; the worktree file is shown relative to the worktree.
    await expect(rows.nth(0)).toContainText('User')
    await expect(rows.nth(0)).toContainText('1,234 tokens')
    await expect(rows.nth(1)).toContainText('ProjectCLAUDE.md958 tokens')
    await noOverflow(page)

    // A project file opens read-only, with no Edit link.
    await rows.nth(1).getByRole('button').click()
    let dialog = page.getByRole('dialog', { name: 'CLAUDE.md' })
    await expect(dialog.getByText('Use ruff.')).toBeVisible()
    await expect(dialog.getByRole('link', { name: 'Edit in Settings' })).toHaveCount(0)
    await expect(dialog.getByRole('textbox')).toHaveCount(0)
    await noOverflow(page)
    await dialog.getByRole('button', { name: 'Close' }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)

    // The user file links to Settings › Memory.
    await rows.nth(0).getByRole('button').click()
    dialog = page.getByRole('dialog')
    await expect(dialog.getByText('Always run the tests.')).toBeVisible()
    await noOverflow(page)
    await dialog.getByRole('link', { name: 'Edit in Settings' }).click()
    await expect(page).toHaveURL(/\/settings\/memory$/)
    await expect(page.getByRole('dialog')).toHaveCount(0)

    const editor = page.getByRole('textbox', { name: 'User memory (CLAUDE.md)' })
    await expect(editor).toHaveValue('# Global\nAlways run the tests.\n')
    await expect(page.getByText(COPY)).toBeVisible()
    await editor.fill('# Global\nAlways run the tests.\nBe brief.\n')
    await page.getByRole('button', { name: 'Save' }).click()

    // Conflict: the current file is shown, the draft is kept until Reload.
    await expect(page.getByText('Changed elsewhere')).toBeVisible()
    await expect(page.getByRole('region', { name: 'Current file' })).toContainText('Edited on rocky.')
    await expect(editor).toHaveValue('# Global\nAlways run the tests.\nBe brief.\n')
    await noOverflow(page)
    await page.getByRole('button', { name: 'Reload' }).click()
    await expect(editor).toHaveValue('# Global\nEdited on rocky.\n')
    await expect(page.getByText('Changed elsewhere')).toHaveCount(0)

    await editor.fill('# Global\nEdited on rocky.\nBe brief.\n')
    await page.getByRole('button', { name: 'Save' }).click()
    await expect(page.getByText(PROPAGATION)).toBeVisible()
    expect(puts.map((p) => p.etag)).toEqual(['e1', 'e2'])
    await noOverflow(page)
  })
}
