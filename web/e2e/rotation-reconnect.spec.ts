/**
 * T045 — WebSocket reconnect + account rotation E2E tests.
 *
 * Prerequisites:
 *   - `npm run build && npm run preview` (or `npm run dev`) running at http://127.0.0.1:4173
 *   - Browsers installed: `npx playwright install`
 *
 * These tests use Playwright's `routeWebSocket` to simulate the server, driving
 * REAL current-protocol frames (batch/replayReset/replayComplete/rotated) built by
 * e2e/fixtures/wsFrames.ts — mirroring server/bridge/ws.py, web/src/ws/client.ts and
 * web/src/ws/liveBatch.ts. The transcript the client rebuilds from those frames is
 * asserted directly, so a regression in replay/reconnect/dedup handling fails the test.
 *
 * This replaces a spec that drove a dead `{type:'output', data:'…'}` vocabulary the
 * client never parsed and only checked navigation chrome (a Back button) — so it
 * passed even if replay was completely broken (audit wave 3, false-confidence e2e).
 *
 * Run: npx playwright test e2e/rotation-reconnect.spec.ts
 */

import { test, expect, type Page } from '@playwright/test'
import { replayFrame, liveFrame, rotated, userTurn, assistantTurn, turnStart, textDelta, wire } from './fixtures/wsFrames'

const SID = 'sess-1'

const MOCK_SESSIONS = [
  {
    id: SID,
    name: 'Work session',
    accountName: 'alice',
    status: 'working',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  },
]

const MOCK_ACCOUNTS = [
  {
    name: 'alice',
    status: 'ok',
    usage: { session: { pct: 30, resetsAt: null }, weekly: { pct: 50, resetsAt: null } },
  },
  {
    name: 'bob',
    status: 'ok',
    usage: { session: { pct: 10, resetsAt: null }, weekly: { pct: 20, resetsAt: null } },
  },
]

// Plain, markdown-safe transcript content so getByText matches the rendered prose
// (assistant text goes through react-markdown; user text renders verbatim).
const U1 = 'first user prompt alpha'
const A1 = 'assistant reply one bravo'
const U2 = 'second user prompt charlie'
const A2 = 'assistant reply two delta'
const LIVE = 'live streamed line echo'

async function interceptApi(page: Page) {
  await page.route('/api/sessions', (route) => route.fulfill({ json: MOCK_SESSIONS }))
  await page.route('/api/accounts', (route) => route.fulfill({ json: MOCK_ACCOUNTS }))
}

type MockWs = { send: (msg: string) => void; close: () => Promise<void> }

async function openWorkSession(page: Page) {
  await expect(page.getByText('Work session')).toBeVisible()
  await page.getByRole('button', { name: /open session work session/i }).click()
}

// Scope transcript-content assertions to the scroll container so they don't also
// match the sr-only aria-live region (aria-label="Session output"), which mirrors
// the LAST assistant text for screen readers — a second, legitimate copy in the DOM.
const transcript = (page: Page) => page.getByTestId('transcript-scroll')

test.describe('T045 — WebSocket reconnect + account rotation', () => {
  test('WS close → reconnect re-attaches and the reconnect replay renders new content', async ({ page }) => {
    await interceptApi(page)

    // The server replays the transcript on EVERY attach (initial + each reconnect
    // re-attach). We grow the transcript between attaches so the post-reconnect
    // replay carries content that was NOT present on the first attach — proving the
    // reconnect replay actually rebuilt the transcript, not just that chrome survived.
    let attachCount = 0
    let currentWs: MockWs | null = null

    await page.routeWebSocket('/ws', (ws) => {
      currentWs = ws as unknown as MockWs
      ws.onMessage((msg) => {
        const parsed = JSON.parse(msg.toString()) as { type: string }
        if (parsed.type !== 'attach') return
        attachCount += 1
        const events =
          attachCount === 1
            ? [...userTurn('t-u1', U1), ...assistantTurn('t-a1', A1)]
            : [...userTurn('t-u1', U1), ...assistantTurn('t-a1', A1),
               ...userTurn('t-u2', U2), ...assistantTurn('t-a2', A2)]
        const totalTurns = attachCount === 1 ? 2 : 4
        ws.send(wire(replayFrame(SID, events, { totalTurns })))
      })
    })

    await page.goto('/')
    await openWorkSession(page)

    // Initial replay rendered the first exchange.
    await expect(transcript(page).getByText(A1)).toBeVisible()
    await expect(transcript(page).getByText(U1)).toBeVisible()

    // Drop the socket — the client reconnects (SC-008) and re-attaches.
    expect(currentWs).not.toBeNull()
    await currentWs!.close()

    // The reconnect replay must render the content that only the second attach
    // carried. Auto-waits through the reconnect backoff.
    await expect(transcript(page).getByText(A2)).toBeVisible({ timeout: 15000 })
    await expect(transcript(page).getByText(U2)).toBeVisible()

    // replayReset fence: the reconnect replay re-delivered U1/A1 too, but the reset
    // that leads it rebuilt the transcript rather than double-appending — each turn
    // appears exactly once (the "sessions duplicating content" bug this guards).
    await expect(transcript(page).getByText(U1)).toHaveCount(1)
    await expect(transcript(page).getByText(A1)).toHaveCount(1)
    expect(attachCount).toBeGreaterThanOrEqual(2)
  })

  test('duplicate tagged live frame is dropped by the seq fence (no double-render)', async ({ page }) => {
    await interceptApi(page)

    // handleLiveBatch keys dedup on the per-attach `seq`: a tagged frame whose seq is
    // not strictly greater than the last applied is dropped before it reaches the
    // reducer (the at-most-once "#477" fence). We stream a live assistant line, then
    // re-send the identical tagged frame (same seq) and assert its text is NOT
    // rendered twice. The textDelta is the LAST event in the turn (highest seq), so an
    // exact retransmit (seq === last-applied) is dropped SILENTLY — no gap, no heal.
    let currentWs: MockWs | null = null
    await page.routeWebSocket('/ws', (ws) => {
      currentWs = ws as unknown as MockWs
      ws.onMessage((msg) => {
        const parsed = JSON.parse(msg.toString()) as { type: string }
        if (parsed.type !== 'attach') return
        // Empty replay (reset + complete) settles the loading state and baselines seq.
        ws.send(wire(replayFrame(SID, [], { totalTurns: 0 })))
        // turnStart seq0, textDelta(LIVE) seq1 → lastSeq becomes 1.
        ws.send(wire(liveFrame(SID, 0, [turnStart('t-live'), textDelta('t-live', LIVE)])))
      })
    })

    await page.goto('/')
    await openWorkSession(page)

    // Exact-text (not element count): a duplicate textDelta doesn't add a NEW element,
    // it APPENDS into the same open text block (LIVE → LIVELIVE), so we must assert the
    // block's exact content, not how many elements contain LIVE.
    await expect(transcript(page).getByText(LIVE)).toHaveText(LIVE)

    // Re-send the textDelta at its ORIGINAL seq (1) — an exact duplicate retransmit.
    // seq(1) is not > last-applied(1), so handleLiveBatch drops it before the reducer.
    expect(currentWs).not.toBeNull()
    currentWs!.send(wire(liveFrame(SID, 1, [textDelta('t-live', LIVE)])))

    // Give the frame time to be (not) applied, then assert the line is still rendered
    // exactly once — a broken dedup would concatenate it into LIVELIVE.
    await page.waitForTimeout(1000)
    await expect(transcript(page).getByText(LIVE)).toHaveText(LIVE)
  })

  test('rotated WS event → the session view records the account switch', async ({ page }) => {
    // NOTE (audit wave 3 finding): the previous "account badge in SessionList updates"
    // test asserted a bare `getByText('bob')`, which matches the always-visible entry
    // in the dashboard's accounts list (Dashboard.tsx) — so it passed without the
    // rotation ever taking effect (a false positive). The dashboard card badge is also
    // not a deterministic target here: the standalone `rotated` cache patch is not
    // observably reflected on the card in a static-mock harness (the 5s sessions poll
    // reconciles it away, since the frame carries no revAt). The rotation's real,
    // deterministic effect is in the SESSION VIEW: `useSessionRuntime` folds a
    // `rotated` frame into the transcript timeline as a rotation divider. We assert
    // that instead, using the real `rotated()` fixture builder.
    await interceptApi(page)

    let currentWs: MockWs | null = null
    await page.routeWebSocket('/ws', (ws) => {
      currentWs = ws as unknown as MockWs
      ws.onMessage((msg) => {
        const parsed = JSON.parse(msg.toString()) as { type: string }
        if (parsed.type !== 'attach') return
        // A minimal replay so the transcript renders (the rotation divider anchors to
        // the last turn).
        ws.send(wire(replayFrame(SID, [...userTurn('t-u1', U1), ...assistantTurn('t-a1', A1)], { totalTurns: 2 })))
      })
    })

    await page.goto('/')
    await openWorkSession(page)
    await expect(transcript(page).getByText(A1)).toBeVisible()

    // Send a real `rotated` frame for the open session.
    expect(currentWs).not.toBeNull()
    currentWs!.send(wire(rotated(SID, 'alice', 'bob')))

    // The transcript timeline shows the rotation divider (RotationDivider role/label).
    await expect(
      page.getByRole('separator', { name: 'Account switched from alice to bob' }),
    ).toBeVisible({ timeout: 5000 })
  })
})
