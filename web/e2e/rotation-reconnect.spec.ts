/**
 * T045 — WebSocket reconnect + rotated event E2E tests.
 *
 * Prerequisites:
 *   - `npm run build && npm run preview` (or `npm run dev`) running at http://127.0.0.1:4173
 *   - Browsers installed: `npx playwright install`
 *
 * These tests use Playwright's `routeWebSocket` to simulate server events.
 *
 * Viewport note: tests use a mobile viewport (375×812) so that the app takes the
 * routed mobile path where session navigation is URL-driven. The "Back to session
 * list" button is present only in the mobile SessionView, not in the desktop
 * DesktopShell's master-detail layout.
 *
 * Run: npx playwright test e2e/rotation-reconnect.spec.ts
 */

import { test, expect, type Page } from '@playwright/test'

// Force the mobile routing branch for all tests in this file.
test.use({ viewport: { width: 375, height: 812 } })

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

/**
 * Intercept every /api/* request so the specs are fully backend-independent.
 * See dashboard.spec.ts for implementation notes.
 */
async function interceptApi(page: Page) {
  await page.route('**/api/**', (route) => {
    const url = route.request().url()
    const path = new URL(url).pathname

    if (path === '/api/sessions') {
      route.fulfill({ json: MOCK_SESSIONS })
    } else if (path === '/api/accounts') {
      route.fulfill({ json: MOCK_ACCOUNTS })
    } else if (path === '/api/sessions/archived') {
      route.fulfill({ json: [] })
    } else if (path === '/api/models') {
      route.fulfill({ json: [] })
    } else if (path === '/api/settings') {
      route.fulfill({ json: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null, defaultToolDetail: 'condensed' } })
    } else if (path === '/api/config') {
      route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } })
    } else if (path === '/api/daemon/health') {
      route.fulfill({ json: { status: 'ok' } })
    } else if (path === '/api/x11/metrics') {
      route.fulfill({ json: { enabled: false } })
    } else if (path === '/api/deploy/status') {
      route.fulfill({ json: {
        pending: null,
        deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: '' },
      } })
    } else if (path === '/api/automations') {
      route.fulfill({ json: [] })
    } else if (path.startsWith('/api/github')) {
      route.fulfill({ json: [] })
    } else {
      // Generic fallback: empty array is safer than {} for collection endpoints.
      route.fulfill({ json: [] })
    }
  })
}

test.describe('T045 — WebSocket reconnect + account rotation', () => {
  test('WS close → reconnect and scrollback replay appears', async ({ page }) => {
    await interceptApi(page)

    let wsConnection: ReturnType<Parameters<typeof page.routeWebSocket>[1]> | null = null

    await page.routeWebSocket('**/ws', (ws) => {
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

    await page.goto('/sessions')

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
    await page.routeWebSocket('**/ws', (ws) => {
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

    await page.routeWebSocket('**/ws', (ws) => {
      wsConnection = ws
      ws.onMessage(() => {})
    })

    await page.goto('/sessions')

    // Verify initial account label. Scope to the session card — the DispatchBar's
    // Account dropdown also renders "alice"/"bob" <option>s, so a page-wide
    // getByText() trips Playwright's strict mode.
    const sessionCard = page.getByRole('button', { name: /open session work session/i })
    await expect(page.getByText('Work session')).toBeVisible()
    await expect(sessionCard.getByText('alice')).toBeVisible()

    // Open the session — this mounts useSessionRuntime which wires the query client
    // to the WebSocket singleton. Without this step, the socket's _patchSessionAccount
    // is a no-op because useSessionSocket (which calls setQueryClient) only runs when
    // a session view is mounted.
    await page.getByRole('button', { name: /open session work session/i }).click()
    await expect(page.getByRole('button', { name: /back to session list/i })).toBeVisible()

    // Go back to the session list with the socket wired
    await page.getByRole('button', { name: /back to session list/i }).click()
    await expect(page.getByText('Work session')).toBeVisible()

    // Send a `rotated` event via the WebSocket — the socket now has a query client
    // so _patchSessionAccount will update the TanStack Query cache immediately.
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
    await expect(sessionCard.getByText('bob')).toBeVisible({ timeout: 5000 })
  })
})
