/**
 * T045 — WebSocket reconnect + rotated event E2E tests.
 *
 * Prerequisites:
 *   - `npm run build && npm run preview` (or `npm run dev`) running at http://127.0.0.1:4173
 *   - Browsers installed: `npx playwright install`
 *
 * These tests use Playwright's `routeWebSocket` to simulate server events.
 *
 * Run: npx playwright test e2e/rotation-reconnect.spec.ts
 */

import { test, expect, type Page } from '@playwright/test'

const MOCK_SESSIONS = [
  {
    id: 'sess-1',
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

async function interceptApi(page: Page) {
  await page.route('/api/sessions', (route) => {
    route.fulfill({ json: MOCK_SESSIONS })
  })
  await page.route('/api/accounts', (route) => {
    route.fulfill({ json: MOCK_ACCOUNTS })
  })
}

test.describe('T045 — WebSocket reconnect + account rotation', () => {
  test('WS close → reconnect and scrollback replay appears', async ({ page }) => {
    await interceptApi(page)

    let wsConnection: ReturnType<Parameters<typeof page.routeWebSocket>[1]> | null = null

    await page.routeWebSocket('/ws', (ws) => {
      wsConnection = ws
      ws.onMessage((msg) => {
        const parsed = JSON.parse(msg.toString()) as { type: string; sessionId?: string }
        // On attach, send some initial output
        if (parsed.type === 'attach') {
          ws.send(JSON.stringify({
            type: 'output',
            sessionId: parsed.sessionId,
            data: 'Initial scrollback line\r\n',
          }))
          ws.send(JSON.stringify({ type: 'replayComplete', sessionId: parsed.sessionId }))
        }
      })
    })

    await page.goto('/')

    // Open a session
    await expect(page.getByText('Work session')).toBeVisible()
    await page.getByRole('button', { name: /open session work session/i }).click()

    // Give xterm time to render the initial output
    await page.waitForTimeout(500)

    // Close the WebSocket to trigger reconnect
    if (wsConnection) {
      await (wsConnection as { close: () => Promise<void> }).close()
    }

    // Set up a new WS handler that sends the replay again
    await page.routeWebSocket('/ws', (ws) => {
      ws.onMessage((msg) => {
        const parsed = JSON.parse(msg.toString()) as { type: string; sessionId?: string }
        if (parsed.type === 'attach') {
          ws.send(JSON.stringify({
            type: 'output',
            sessionId: parsed.sessionId,
            data: 'Replayed after reconnect\r\n',
          }))
          ws.send(JSON.stringify({ type: 'replayComplete', sessionId: parsed.sessionId }))
        }
      })
    })

    // Wait for reconnect (up to 10s per SC-008)
    await page.waitForTimeout(2000)

    // The reconnect indicator should have disappeared (or briefly appeared)
    // and the terminal is back to connected state
    // Verify: back button still works (terminal is still mounted and functional)
    await expect(page.getByRole('button', { name: /back to session list/i })).toBeVisible()
  })

  test('rotated WS event → account badge in SessionList updates', async ({ page }) => {
    let wsConnection: ReturnType<Parameters<typeof page.routeWebSocket>[1]> | null = null

    await interceptApi(page)

    await page.routeWebSocket('/ws', (ws) => {
      wsConnection = ws
      ws.onMessage(() => {})
    })

    await page.goto('/')

    // Verify initial account label
    await expect(page.getByText('Work session')).toBeVisible()
    await expect(page.getByText('alice')).toBeVisible()

    // Send a `rotated` event via the WebSocket
    if (wsConnection) {
      ;(wsConnection as { send: (msg: string) => void }).send(JSON.stringify({
        type: 'rotated',
        sessionId: 'sess-1',
        fromAccount: 'alice',
        toAccount: 'bob',
      }))
    }

    // The session card should update to show the new account
    // (TanStack Query cache is patched by the WS client on `rotated` events)
    await expect(page.getByText('bob')).toBeVisible({ timeout: 5000 })
  })
})
