/**
 * Finish-line row 4, task 6 — E2E smoke for the Delivery section (finish-line
 * spec §4.2, §4.4) now that Settings › Automations is retired (R1).
 *
 * Prerequisites (same harness as `roadmap.spec.ts`):
 *   - `npm run build && npm run preview` at http://127.0.0.1:4173 — Playwright's
 *     `webServer` block starts this for you.
 *   - Browsers installed: `npx playwright install chromium`.
 *   - There is NO backend. Every `/api/*` call is intercepted below, so nothing
 *     here can be seeded through a real endpoint; the automations fixture is
 *     inlined (a copy of `web/src/features/delivery/__fixtures__/automations.ts`,
 *     which the specs cannot import because Playwright's transform does not
 *     resolve the `@/` Vite alias).
 *
 * Run: npx playwright test e2e/delivery.spec.ts
 */

import { test, expect, type Page, type Route } from '@playwright/test'

// ── Fixtures ──────────────────────────────────────────────────────────────────

/** Mirrors `web/src/features/delivery/__fixtures__/automations.ts`. */
const AUTOMATIONS = [
  {
    id: 'pr-doctor', name: 'PR doctor', description: 'Fixes failing PRs.', enabled: true, mode: 'auto',
    modes: ['off', 'propose', 'auto'], status: { text: '2 PRs failing', level: 'warn', counts: null },
    enginePolicy: 'claude_only', operatorTriggered: false,
    configSchema: [
      { key: 'auto_fix', label: 'Auto-fix', type: 'bool', default: true, help: 'Open fix sessions without asking.', choices: null },
      { key: 'enabled_repos', label: 'Repositories', type: 'repo-multiselect', default: [], help: '', choices: null },
    ],
    config: { auto_fix: true, enabled_repos: ['o/r'] }, schedule: null, lastActiveAt: '2026-09-11T10:00:00Z',
    activeSessions: 1, workClass: 'maintenance',
  },
  {
    id: 'backlog-runner', name: 'Backlog runner', description: 'Dispatches eligible issues.', enabled: true, mode: 'on',
    modes: ['off', 'on'], status: { text: 'Idle', level: 'idle', counts: null },
    enginePolicy: 'claude_only', operatorTriggered: false, configSchema: [], config: {}, schedule: null,
    lastActiveAt: null, activeSessions: 0, workClass: 'development',
  },
  {
    id: 'pm-classify', name: 'PM classify', description: 'Classifies issues on request.', enabled: false, mode: null,
    modes: [], status: { text: 'Idle', level: 'idle', counts: null }, enginePolicy: 'claude_only', operatorTriggered: true,
    configSchema: [], config: {}, schedule: null, lastActiveAt: null, activeSessions: 0, workClass: 'maintenance',
  },
]

const SETTINGS = {
  defaultPermissionMode: 'interactive',
  thinkingCollapsed: false,
  repoRoot: null,
  defaultToolDetail: 'auto',
  maxArchivedSessions: null,
  archiveHome: null,
  maxActiveSessions: null,
  autoArchiveEnabled: true,
  modelApprovalAction: null,
  modelApprovalDenylist: null,
  acceptedPermissionModes: ['interactive', 'auto', 'allowlist', 'acceptEdits', 'full-auto', 'bypassPermissions'],
  effectiveDefaults: {
    repoRoot: '/home/user/GitHub',
    archiveHome: null,
    modelApprovalAction: 'require_approval',
    modelApprovalDenylist: 'fable',
    maxActiveSessions: 5,
  },
  managed: [],
  about: { gitSha: null, hostName: 'test-host' },
}

/**
 * One catch-all handler, dispatching on pathname — the `roadmap.spec.ts`
 * pattern. `/automations` is the list the Delivery list column and the
 * automation detail both read; the per-automation `actionable`/`events`/
 * `sessions` reads are collections, so they answer `[]`.
 */
async function interceptApi(page: Page) {
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname

    if (path === '/api/automations') {
      route.fulfill({ json: AUTOMATIONS })
    } else if (/^\/api\/automations\/[^/]+\/(actionable|events|sessions)$/.test(path)) {
      route.fulfill({ json: [] })
    } else if (path === '/api/pipeline/repos') {
      route.fulfill({ json: {} })
    } else if (path === '/api/settings') {
      route.fulfill({ json: SETTINGS })
    } else if (path === '/api/config') {
      route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } })
    } else if (path === '/api/daemon/health') {
      route.fulfill({ json: { status: 'ok' } })
    } else if (path === '/api/x11/metrics') {
      route.fulfill({ json: { enabled: false } })
    } else if (path === '/api/deploy/status') {
      route.fulfill({
        json: { pending: null, deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: '' } },
      })
    } else if (path === '/api/fleet/hosts') {
      route.fulfill({ json: { hosts: [] } })
    } else {
      // Collections (sessions, accounts, models, github…).
      route.fulfill({ json: [] })
    }
  })

  await page.routeWebSocket('**/ws', (ws) => {
    ws.onMessage(() => {})
  })
}

const pathOf = (page: Page) => new URL(page.url()).pathname

// ── Desktop (1440×900): toggling the enable switch reaches the server ─────────

test.describe('Delivery — desktop', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test.beforeEach(async ({ page }) => {
    await interceptApi(page)
  })

  test('opens an automation and toggles its enable switch', async ({ page }) => {
    const patched: Array<{ path: string; body: unknown }> = []
    // Registered AFTER `interceptApi`'s catch-all, so it wins for this one path
    // (Playwright tries the most-recently-added matching route first).
    await page.route('**/api/automations/pr-doctor', (route: Route) => {
      if (route.request().method() === 'PATCH') {
        patched.push({ path: new URL(route.request().url()).pathname, body: route.request().postDataJSON() })
        route.fulfill({ json: { ...AUTOMATIONS[0], enabled: false } })
      } else {
        route.fulfill({ json: [] })
      }
    })

    await page.goto('/delivery')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Delivery')

    await page.getByRole('button', { name: 'Open automation PR doctor' }).click()

    await expect.poll(() => pathOf(page)).toBe('/delivery/automations/pr-doctor')
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('PR doctor')

    const toggle = page.getByRole('switch', { name: 'PR doctor enabled' })
    await expect(toggle).toHaveAttribute('aria-checked', 'true')
    await toggle.click()

    await expect.poll(() => patched).toEqual([
      { path: '/api/automations/pr-doctor', body: { enabled: false } },
    ])
  })
})

// ── Phone (390×844): the detail takes the single content band ─────────────────

test.describe('Delivery — phone', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test.beforeEach(async ({ page }) => {
    await interceptApi(page)
  })

  test('the list owns the band until an automation is open, then the detail does, and Back returns to the list', async ({ page }) => {
    await page.goto('/delivery')

    // Nothing open at this width: the list has the single content band and the
    // page is hidden (attribute, so it leaves the a11y tree too) — hence no
    // `h1` here, unlike desktop.
    const list = page.getByRole('list', { name: 'Automations' })
    await expect(list).toBeVisible()
    await expect(page.locator('[data-page-variant]')).toBeHidden()
    await expect(page.getByRole('heading', { level: 1 })).toHaveCount(0)

    await page.getByRole('button', { name: 'Open automation PR doctor' }).click()

    await expect(page.getByRole('heading', { level: 1 })).toHaveText('PR doctor')
    expect(pathOf(page)).toBe('/delivery/automations/pr-doctor')
    // The band changed hands: the list is gone once something is open (it is
    // not rendered at all on phone while an item is open, not merely hidden).
    await expect(list).toHaveCount(0)
    await expect(page.locator('[data-page-variant]')).toBeVisible()

    await page.goBack()
    await expect.poll(() => pathOf(page)).toBe('/delivery')
    await expect(list).toBeVisible()
    await expect(page.getByRole('heading', { level: 1 })).toHaveCount(0)
  })
})
