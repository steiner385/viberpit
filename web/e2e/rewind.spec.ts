/**
 * Rewind to here (D-9): the per-message menu rewinds a prompt after a confirm built from
 * the dry run (files, deletions marked, `+ −`, the caveat; or the conversation-only line,
 * which sends `mode: "conversation"`). The `rewound` event cuts the turns and leaves the
 * divider; its Undo posts the rewindId, and `rewindUndone` brings the turns back through
 * a fresh replay. Edit & resend refills the composer, a 409 says why, `/rewind` picks a
 * prompt, an error card's Rewind & retry rewinds to the failed prompt and sends it again
 * once, and an engine without the capability offers no rewind. The API is mocked with
 * `page.route`, the socket with `page.routeWebSocket`, like `fork.spec.ts`.
 */
import { test, expect, type Page, type Request, type WebSocketRoute } from '@playwright/test'
import { replayFrame, liveFrame, userTurn, assistantTurn, wire } from './fixtures/wsFrames'
import type { SessionEvent } from '../src/session/events'

const sid = 'rewind-session'
// A replayed user turn's id IS its CLI uuid — what makes it a rewind target.
const U1 = '11111111-1111-4111-8111-111111111111'
const U2 = '22222222-2222-4222-8222-222222222222'

const HISTORY: SessionEvent[] = [
  ...userTurn(U1, 'First question'),
  ...assistantTurn('t1', 'First answer'),
  ...userTurn(U2, 'Second question'),
  ...assistantTurn('t2', 'Second answer'),
]

const REWIND_CMD = { name: 'rewind', description: 'Rewind this session to one of your messages',
  argumentHint: null, kind: 'client', source: 'app' }

const CAPS = { models: true, reasoningEffort: true, steer: true, interrupt: true, approvals: true,
  questions: true, adopt: true, selfModelSwitch: true, subagents: true, attachments: true,
  accountRotation: true, accountSwitchNewThread: false, fork: false }

const PREVIEW = { files: ['src/app.ts', 'src/new.ts'], deleted: ['src/new.ts'], insertions: 12, deletions: 3,
  checkpointAvailable: true }
const RESULT = { rewindId: 'rw-1', prefill: 'Second question', filesRestored: ['src/app.ts', 'src/new.ts'] }
const CAVEAT = 'Changes made by shell commands are not restored, and edits made outside Claude since that message are overwritten.'

type Reply = { status: number; json: unknown }

interface Opened {
  posts: Request[]; undos: Request[]; prompts: string[]; attaches: () => number; socket: () => WebSocketRoute
}

async function openSession(
  page: Page, opts: { rewind?: boolean; preview?: Reply; reply?: Reply; history?: SessionEvent[] } = {},
): Promise<Opened> {
  const {
    rewind = true,
    history = HISTORY,
    preview = { status: 200, json: PREVIEW },
    reply = { status: 200, json: RESULT },
  } = opts
  const posts: Request[] = []
  const undos: Request[] = []
  const prompts: string[] = []
  let attaches = 0
  let socket: WebSocketRoute | null = null
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Rewind me', accountName: 'alice', engine: 'claude_code', status: 'idle', cwd: `/home/u/worktrees/${sid}`,
    createdAt: '2026-11-03T10:00:00Z', updatedAt: '2026-11-03T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: {
    repoRoot: '', adoptionEnabled: false,
    engines: [{ engine: 'claude_code', available: true, unavailableReasons: {}, capabilities: { ...CAPS, rewind } }],
  } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/api/sessions/*/diff**', (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  await page.route(`**/api/sessions/${sid}/rewind`, (route) => {
    posts.push(route.request())
    const r = (route.request().postDataJSON() as { dryRun?: boolean }).dryRun ? preview : reply
    return route.fulfill({ status: r.status, json: r.json })
  })
  await page.route(`**/api/sessions/${sid}/rewind/undo`, (route) => {
    undos.push(route.request())
    return route.fulfill({ json: { ok: true, skipped: [] } })
  })
  await page.routeWebSocket('**/ws', (ws) => {
    socket = ws
    ws.onMessage((msg) => {
      const frame = JSON.parse(msg.toString()) as { type: string; sessionId?: string; text?: string }
      if (frame.type === 'prompt' && frame.sessionId === sid) prompts.push(frame.text ?? '')
      if (frame.type !== 'attach' || frame.sessionId !== sid) return
      attaches += 1
      ws.send(wire(replayFrame(sid, history, { totalTurns: 4 })))
      ws.send(JSON.stringify({ type: 'commands', sessionId: sid, commands: rewind ? [REWIND_CMD] : [] }))
    })
  })
  await page.goto('/')
  await page.getByRole('button', { name: /open session rewind me/i }).click()
  await expect(page.getByText('Second answer')).toBeVisible()
  return { posts, undos, prompts, attaches: () => attaches, socket: () => socket! }
}

/** Open the "⋯" menu of the prompt whose text is `text`. */
async function openMenu(page: Page, text: string) {
  const turn = page.locator('div:has(> button[aria-label^="Message actions"])').filter({ hasText: text }).first()
  await turn.hover()
  await turn.getByRole('button', { name: 'Message actions — your prompt' }).click()
}

test('rewinding confirms from the dry run, cuts the turns, and Undo brings them back', async ({ page }) => {
  const { posts, undos, attaches, socket } = await openSession(page)
  await openMenu(page, 'Second question')
  await page.getByRole('menuitem', { name: 'Rewind to here' }).click()

  const dialog = page.getByRole('dialog', { name: 'Rewind to this message?' })
  await expect(dialog.getByText('src/app.ts')).toBeVisible()
  await expect(dialog.getByRole('listitem').filter({ hasText: 'src/new.ts' })).toContainText('(deleted)')
  await expect(dialog.getByText('+12')).toBeVisible()
  await expect(dialog.getByText('−3')).toBeVisible()
  await expect(dialog.getByText(CAVEAT)).toBeVisible()
  await dialog.getByRole('button', { name: 'Rewind' }).click()
  await expect(dialog).toBeHidden()
  expect(posts.map((p) => p.postDataJSON())).toEqual([
    expect.objectContaining({ toTurnId: U2, mode: 'files_and_conversation', dryRun: true }),
    expect.objectContaining({ toTurnId: U2, mode: 'files_and_conversation', dryRun: false }),
  ])

  // The server's `rewound` event cuts the prompt and its reply; the divider takes their place.
  socket().send(wire(liveFrame(sid, 0, [{ type: 'rewound', rewindId: 'rw-1', toTurnId: U2, promptUuid: U2, filesRestored: RESULT.filesRestored }])))
  const transcript = page.getByTestId('transcript-scroll')
  await expect(transcript.getByText('Rewound — 2 files restored')).toBeVisible()
  await expect(transcript.getByText('Second question')).toHaveCount(0)
  await expect(transcript.getByText('Second answer')).toHaveCount(0)
  await expect(transcript.getByText('First answer')).toBeVisible()

  await page.getByRole('button', { name: 'Undo rewind' }).click()
  await expect.poll(() => undos.length).toBe(1)
  expect(undos[0].postDataJSON()).toEqual({ rewindId: 'rw-1' })
  // `rewindUndone` drops the divider and re-attaches: the replay shows the turns again.
  const before = attaches()
  socket().send(wire(liveFrame(sid, 1, [{ type: 'rewindUndone', rewindId: 'rw-1' }])))
  await expect.poll(attaches).toBe(before + 1)
  await expect(transcript.getByText('Second answer')).toBeVisible()
  await expect(transcript.getByText(/Rewound —/)).toHaveCount(0)
})

test('without a checkpoint it says only the conversation rewinds, and sends that mode', async ({ page }) => {
  const { posts } = await openSession(page, { preview: { status: 200, json: {
    files: [], deleted: [], insertions: 0, deletions: 0, checkpointAvailable: false } } })
  await openMenu(page, 'Second question')
  await page.getByRole('menuitem', { name: 'Rewind to here' }).click()
  const dialog = page.getByRole('dialog', { name: 'Rewind to this message?' })
  await expect(dialog.getByText("Files can't be restored for this message — only the conversation will be rewound.")).toBeVisible()
  await dialog.getByRole('button', { name: 'Rewind' }).click()
  await expect.poll(() => posts.length).toBe(2)
  expect(posts[1].postDataJSON()).toMatchObject({ toTurnId: U2, mode: 'conversation', dryRun: false })
})

test('Edit & resend rewinds and puts the prompt back in the composer', async ({ page }) => {
  await openSession(page)
  await openMenu(page, 'Second question')
  await page.getByRole('menuitem', { name: 'Edit & resend' }).click()
  const dialog = page.getByRole('dialog', { name: 'Rewind and edit this message?' })
  await dialog.getByRole('button', { name: 'Rewind and edit' }).click()
  await expect(page.getByRole('textbox', { name: /message/i })).toHaveValue('Second question')
})

test('a refused rewind says why', async ({ page }) => {
  await openSession(page, { reply: { status: 409, json: { detail: { code: 'REWIND_BUSY', message: 'busy' } } } })
  await openMenu(page, 'Second question')
  await page.getByRole('menuitem', { name: 'Rewind to here' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Rewind' }).click()
  await expect(page.getByText("Claude is working or waiting on you — rewind once that's settled")).toBeVisible()
})

test('/rewind picks a prompt and runs the same flow', async ({ page }) => {
  const { posts } = await openSession(page)
  const box = page.getByRole('textbox', { name: /message/i })
  await box.fill('/rewind')
  await box.press('Enter')
  const menu = page.getByRole('menu', { name: 'Rewind to a message' })
  // Newest first.
  await expect(menu.getByRole('menuitem').first()).toContainText('Second question')
  await menu.getByRole('menuitem', { name: 'First question' }).click()
  await expect(page.getByRole('dialog', { name: 'Rewind to this message?' })).toBeVisible()
  expect(posts[0].postDataJSON()).toMatchObject({ toTurnId: U1, dryRun: true })
})

test('an engine that cannot rewind offers no rewind', async ({ page }) => {
  await openSession(page, { rewind: false })
  await openMenu(page, 'Second question')
  await expect(page.getByRole('menuitem', { name: 'Copy as Markdown' })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: 'Rewind to here' })).toHaveCount(0)
  await expect(page.getByRole('menuitem', { name: 'Edit & resend' })).toHaveCount(0)
})

// The second turn failed: its reply carries an error, so its card offers Rewind & retry.
const FAILED: SessionEvent[] = [
  ...HISTORY.slice(0, -1),
  { type: 'error', turnId: 't2', code: 'api_error', message: 'Overloaded — try again' },
  { type: 'turnEnd', turnId: 't2' },
]

test("Rewind & retry on an error card rewinds to the failed prompt and sends it again, once", async ({ page }) => {
  const { posts, prompts, socket } = await openSession(page, { history: FAILED })
  const transcript = page.getByTestId('transcript-scroll')
  await transcript.getByRole('button', { name: 'Rewind & retry' }).click()
  const dialog = page.getByRole('dialog', { name: 'Rewind and retry this message?' })
  await expect(dialog.getByText('src/app.ts')).toBeVisible()
  await expect(dialog.getByText("Your message is sent again right after the rewind, so this rewind can't be undone.")).toBeVisible()
  await dialog.getByRole('button', { name: 'Rewind and retry' }).click()
  await expect(dialog).toBeHidden()
  expect(posts.map((p) => p.postDataJSON())).toEqual([
    expect.objectContaining({ toTurnId: U2, dryRun: true }),
    expect.objectContaining({ toTurnId: U2, mode: 'files_and_conversation', dryRun: false }),
  ])
  // The resend waits for the cut: nothing goes out before the server's `rewound`.
  expect(prompts).toEqual([])
  socket().send(wire(liveFrame(sid, 0, [{ type: 'rewound', rewindId: 'rw-1', toTurnId: U2, promptUuid: U2, filesRestored: RESULT.filesRestored }])))
  await expect.poll(() => prompts).toEqual([RESULT.prefill])
  await expect(transcript.getByText('Second answer')).toHaveCount(0)
  // Settle: still exactly one send.
  await page.waitForTimeout(300)
  expect(prompts).toEqual([RESULT.prefill])
})

test('a refused Rewind & retry says why and sends nothing', async ({ page }) => {
  const { prompts } = await openSession(page, {
    history: FAILED, reply: { status: 409, json: { detail: { code: 'REWIND_SPLITS_TOOL_CALL', message: 'split' } } } })
  await page.getByTestId('transcript-scroll').getByRole('button', { name: 'Rewind & retry' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Rewind and retry' }).click()
  await expect(page.getByText("Can't rewind here — it would split a tool call from its result")).toBeVisible()
  await page.waitForTimeout(300)
  expect(prompts).toEqual([])
})

test('only the latest prompt\'s failure offers Rewind & retry', async ({ page }) => {
  // The first turn failed, the second succeeded: the old failure offers nothing.
  await openSession(page, { history: [
    ...userTurn(U1, 'First question'),
    { type: 'turnStart', turnId: 't1' },
    { type: 'error', turnId: 't1', code: 'api_error', message: 'Overloaded — try again' },
    { type: 'turnEnd', turnId: 't1' },
    ...userTurn(U2, 'Second question'),
    ...assistantTurn('t2', 'Second answer'),
  ] })
  await expect(page.getByTestId('transcript-scroll').getByText('Overloaded — try again')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Rewind & retry' })).toHaveCount(0)
})

test('an engine that cannot rewind offers no Rewind & retry', async ({ page }) => {
  await openSession(page, { rewind: false, history: FAILED })
  await expect(page.getByTestId('transcript-scroll').getByText('Overloaded — try again')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Rewind & retry' })).toHaveCount(0)
})
