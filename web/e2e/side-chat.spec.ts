/**
 * Side chat (D-12): inside a session, `/btw <question>` opens the Assistant drawer, whose
 * header says what it is about ("About: <session title>"), and sends the question there
 * as exactly one prompt frame carrying the session as its context. `/btw` alone opens
 * the drawer and sends nothing. The API is mocked with `page.route`, the socket with
 * `page.routeWebSocket`, like `rewind.spec.ts`.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, userTurn, assistantTurn, wire } from './fixtures/wsFrames'

const sid = 'side-chat-session'
const asst = 'assistant-session'

const BTW_CMD = { name: 'btw', description: 'Ask the Assistant about this session in the side chat',
  argumentHint: '<question>', kind: 'client', source: 'app' }

interface PromptFrame { sessionId: string; text: string; context?: { route?: string; sessionId?: string } }

async function openSession(page: Page): Promise<{ prompts: PromptFrame[] }> {
  const prompts: PromptFrame[] = []
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Fix the flaky login test', accountName: 'alice', engine: 'claude_code', status: 'idle',
    cwd: `/home/u/worktrees/${sid}`, createdAt: '2026-11-03T10:00:00Z', updatedAt: '2026-11-03T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: {
    repoRoot: '', adoptionEnabled: false,
    engines: [{ engine: 'claude_code', available: true, unavailableReasons: {}, capabilities: {} }],
  } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/api/sessions/*/diff**', (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route('**/api/assistant/status', (route) => route.fulfill({ json: {
    enabled: true, sessionId: asst, model: 'sonnet', operatorTools: false, engine: 'claude_code',
  } }))
  await page.routeWebSocket('**/ws', (ws) => {
    ws.onMessage((msg) => {
      const frame = JSON.parse(msg.toString()) as PromptFrame & { type: string }
      if (frame.type === 'prompt') prompts.push(frame)
      if (frame.type !== 'attach') return
      if (frame.sessionId === sid) {
        ws.send(wire(replayFrame(sid, [...userTurn('u1', 'Make the test pass'), ...assistantTurn('a1', 'Done')],
          { totalTurns: 2 })))
        ws.send(JSON.stringify({ type: 'commands', sessionId: sid, commands: [BTW_CMD] }))
      } else if (frame.sessionId === asst) {
        ws.send(wire(replayFrame(asst, [], { totalTurns: 0 })))
        // The assistant's own catalog never lists /btw (Review Focus 4).
        ws.send(JSON.stringify({ type: 'commands', sessionId: asst, commands: [] }))
      }
    })
  })
  await page.goto('/')
  await page.getByRole('button', { name: /open session fix the flaky login test/i }).click()
  await expect(page.getByText('Make the test pass')).toBeVisible()
  return { prompts }
}

const dockBox = (page: Page) => page.locator('[data-region="app.dock"]').getByRole('textbox', { name: /message/i })

test('/btw <question> opens the Assistant about this session and sends the question once', async ({ page }) => {
  const { prompts } = await openSession(page)
  await dockBox(page).fill('/btw what changed?')
  await dockBox(page).press('Enter')

  const drawer = page.getByRole('dialog', { name: 'Assistant' })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByTestId('assistant-scope')).toHaveText('About: Fix the flaky login test')
  await expect.poll(() => prompts.filter((p) => p.text === 'what changed?').length).toBe(1)
  const [sent] = prompts.filter((p) => p.text === 'what changed?')
  expect(sent.sessionId).toBe(asst)
  expect(sent.context?.sessionId).toBe(sid)
  // The session itself got nothing, and the composer was cleared.
  expect(prompts.filter((p) => p.sessionId === sid)).toEqual([])
  await expect(dockBox(page)).toHaveValue('')
  // Still exactly one after the drawer settles.
  await page.waitForTimeout(500)
  expect(prompts.filter((p) => p.text === 'what changed?')).toHaveLength(1)
})

test('/btw alone opens the Assistant and sends nothing', async ({ page }) => {
  const { prompts } = await openSession(page)
  // `/btw ` — what picking it from the slash menu leaves — so Enter sends it bare.
  await dockBox(page).fill('/btw ')
  await dockBox(page).press('Enter')
  await expect(page.getByRole('dialog', { name: 'Assistant' })).toBeVisible()
  await page.waitForTimeout(500)
  expect(prompts).toEqual([])
})
