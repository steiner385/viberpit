/**
 * Fork from here (D-8): the per-message menu forks a reply at its own turn and a
 * prompt at the reply before it (prefilling the child's composer), opens the child,
 * and says its files come from the current worktree. A 409 says why, `/fork` forks
 * at the latest reply, and an engine without the capability offers no fork. The API
 * is mocked with `page.route`, like the other session specs.
 */
import { test, expect, type Page, type Request } from '@playwright/test'
import { replayFrame, userTurn, assistantTurn, wire } from './fixtures/wsFrames'
import type { SessionEvent } from '../src/session/events'

const sid = 'fork-source'
const child = 'fork-child'

const anchor = (turnId: string, uuid: string): SessionEvent => ({ type: 'turnAnchor', turnId, uuid })

const HISTORY: SessionEvent[] = [
  ...userTurn('u1', 'First question'),
  ...assistantTurn('t1', 'First answer'),
  anchor('t1', 'uuid-1'),
  ...userTurn('u2', 'Second question'),
  ...assistantTurn('t2', 'Second answer'),
  anchor('t2', 'uuid-2'),
]

const FORK_CMD = { name: 'fork', description: 'Fork this session at the latest reply into a new one',
  argumentHint: null, kind: 'client', source: 'app' }

const CAPS = { models: true, reasoningEffort: true, steer: true, interrupt: true, approvals: true,
  questions: true, adopt: true, selfModelSwitch: true, subagents: true, attachments: true,
  accountRotation: true, accountSwitchNewThread: false }

type ForkReply = { status: number; json: unknown }

async function openSession(page: Page, opts: { fork?: boolean; reply?: ForkReply } = {}) {
  const { fork = true, reply = { status: 201, json: { sessionId: child, prefill: null } } } = opts
  const posts: Request[] = []
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  const row = (id: string, name: string) => ({
    id, name, accountName: 'alice', engine: 'claude_code', status: 'idle', cwd: `/home/u/worktrees/${id}`,
    createdAt: '2026-11-02T10:00:00Z', updatedAt: '2026-11-02T10:00:00Z',
  })
  await page.route('**/api/sessions', (route) => route.fulfill({
    json: [row(sid, 'Fork source'), row(child, 'Fork of Fork source')],
  }))
  await page.route('**/api/config', (route) => route.fulfill({ json: {
    repoRoot: '', adoptionEnabled: false,
    engines: [{ engine: 'claude_code', available: true, unavailableReasons: {}, capabilities: { ...CAPS, fork } }],
  } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/api/sessions/*/diff**', (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route(`**/api/sessions/${sid}/fork`, (route) => {
    posts.push(route.request())
    return route.fulfill({ status: reply.status, json: reply.json })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string; sessionId?: string }
    if (frame.type !== 'attach') return
    if (frame.sessionId === sid) {
      ws.send(wire(replayFrame(sid, HISTORY, { totalTurns: 4 })))
      ws.send(JSON.stringify({ type: 'commands', sessionId: sid, commands: fork ? [FORK_CMD] : [] }))
    } else if (frame.sessionId) {
      ws.send(wire(replayFrame(frame.sessionId, [], { totalTurns: 0 })))
    }
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session fork source/i }).click()
  await expect(page.getByText('Second answer')).toBeVisible()
  return posts
}

/** Open the "⋯" menu of the turn whose text is `text`. */
async function openMenu(page: Page, text: string, who: 'reply' | 'your prompt') {
  // A turn's root holds its menu trigger as a direct child (MessageActions' reveal rule).
  const turn = page.locator('div:has(> button[aria-label^="Message actions"])').filter({ hasText: text }).first()
  await turn.hover()
  await turn.getByRole('button', { name: `Message actions — ${who}` }).click()
}

const SUCCESS = 'Forked — files start from the current worktree, not this message'

test('forking a reply opens the child and says where its files come from', async ({ page }) => {
  const posts = await openSession(page)
  await openMenu(page, 'First answer', 'reply')
  await page.getByRole('menuitem', { name: 'Fork from here' }).click()
  await expect(page).toHaveURL(new RegExp(`/sessions/${child}`))
  await expect(page.getByText(SUCCESS)).toBeVisible()
  expect(posts).toHaveLength(1)
  expect(posts[0].postDataJSON()).toMatchObject({ atTurnId: 't1', mode: 'reply' })
})

test('forking a prompt cuts at the reply before it and prefills the child composer', async ({ page }) => {
  const posts = await openSession(page, { reply: { status: 201, json: { sessionId: child, prefill: 'Second question' } } })
  await openMenu(page, 'Second question', 'your prompt')
  await page.getByRole('menuitem', { name: 'Fork from here' }).click()
  await expect(page).toHaveURL(new RegExp(`/sessions/${child}`))
  await expect(page.getByRole('textbox', { name: /message/i })).toHaveValue('Second question')
  expect(posts[0].postDataJSON()).toMatchObject({ atTurnId: 't1', mode: 'message', prefill: 'Second question' })
})

test('a refused fork says why and stays on the session', async ({ page }) => {
  await openSession(page, { reply: { status: 409, json: { detail: {
    code: 'FORK_AT_CAPACITY', message: 'full' } } } })
  await openMenu(page, 'Second answer', 'reply')
  await page.getByRole('menuitem', { name: 'Fork from here' }).click()
  await expect(page.getByText('Too many sessions are running — fork once one finishes')).toBeVisible()
  await expect(page).toHaveURL(new RegExp(`/sessions/${sid}`))
})

test('/fork forks at the latest reply', async ({ page }) => {
  const posts = await openSession(page)
  const box = page.getByRole('textbox', { name: /message/i })
  await box.fill('/fork')
  await box.press('Enter')
  await expect(page).toHaveURL(new RegExp(`/sessions/${child}`))
  expect(posts[0].postDataJSON()).toMatchObject({ atTurnId: 't2', mode: 'reply' })
})

test('an engine that cannot fork offers no fork', async ({ page }) => {
  await openSession(page, { fork: false })
  await openMenu(page, 'Second answer', 'reply')
  await expect(page.getByRole('menuitem', { name: 'Copy as Markdown' })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: 'Fork from here' })).toHaveCount(0)
})
