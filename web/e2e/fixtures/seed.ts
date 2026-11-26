/**
 * Deterministic seeded backend for the cross-browser gates (#1412, G14).
 *
 * Every spec here runs with NO backend: `/api/*` is answered from the constants
 * below and the WebSocket is accepted and left silent. The inputs are fixed on
 * purpose — a frozen clock, stable ids, a fixed ordering, synthetic names — so a
 * screenshot, a trace or an axe report from one engine is comparable with the
 * same artifact from another, and so none of it can carry a real account, prompt
 * or transcript (the E5 spec §3 "evidence is sanitized" gate).
 *
 * Playwright's transform does not resolve the `@/` Vite alias, so these shapes
 * mirror `web/src/test/factories.ts` instead of importing it.
 */
import type { Page } from '@playwright/test'

/** The instant every seeded page believes it is. Fixed → relative times never drift. */
export const SEED_NOW = '2026-11-01T12:00:00Z'

export const SEED_SESSIONS = [
  {
    id: 'seed-working', name: 'Refactor the importer', accountName: 'demo', repo: 'demo-org/demo-repo',
    status: 'working', createdAt: '2026-11-01T10:00:00Z', updatedAt: '2026-11-01T11:55:00Z',
  },
  {
    id: 'seed-handed-back', name: 'Choose the retry budget', accountName: 'demo', repo: 'demo-org/demo-repo',
    status: 'handed_back', handBackReason: 'Need a decision on the retry budget',
    createdAt: '2026-11-01T09:00:00Z', updatedAt: '2026-11-01T09:30:00Z',
  },
  {
    id: 'seed-idle', name: 'Write the release notes', accountName: 'demo', repo: 'demo-org/demo-repo',
    status: 'idle', createdAt: '2026-11-01T08:00:00Z', updatedAt: '2026-11-01T08:45:00Z',
  },
] as const

export const SEED_ACCOUNTS = [
  {
    name: 'demo',
    status: 'ok',
    usage: {
      provider: 'anthropic', fetchedAt: null, availability: 'available',
      windows: [
        { key: 'session', label: 'session', pct: 30, resetsAt: null, durationSeconds: 18000 },
        { key: 'weekly', label: 'weekly', pct: 50, resetsAt: null, durationSeconds: 604800 },
      ],
    },
  },
] as const

/** The FULL settings payload — the Settings sections read every one of these keys. */
export const SEED_SETTINGS = {
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
    repoRoot: '/home/user/projects',
    archiveHome: null,
    modelApprovalAction: 'require_approval',
    modelApprovalDenylist: 'fable',
    globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] },
  },
  managed: [],
  about: { gitSha: null, hostName: 'demo-host' },
} as const

type Json = unknown
/** Per-path response overrides; a path not listed falls through to the seeded default. */
export type SeedOverrides = Record<string, Json>

/** What each endpoint the shell touches on first paint answers with. */
function seededResponse(path: string, overrides: SeedOverrides): Json {
  if (path in overrides) return overrides[path]
  switch (path) {
    case '/api/sessions': return SEED_SESSIONS
    case '/api/accounts': return SEED_ACCOUNTS
    case '/api/settings': return SEED_SETTINGS
    case '/api/config': return { repoRoot: '', adoptionEnabled: false }
    case '/api/daemon/health': return { status: 'ok' }
    case '/api/x11/metrics': return { enabled: false }
    case '/api/deploy/status':
      return { pending: null, deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: '' } }
    case '/api/github/repos': return [{ nameWithOwner: 'demo-org/demo-repo', defaultBranch: 'main' }]
    // One installed plugin: Settings › Plugins renders its enable control, a native
    // `<input type="checkbox" role="switch">` (an analyzer-limitation category, see
    // docs/product/ui-a11y-analyzer-limitations.md).
    case '/api/library/plugins':
      return {
        plugins: [{
          id: 'lint@demo', name: 'lint', marketplace: 'demo', enabled: true, version: '1.0.0', autoUpdate: false,
          description: 'Lints things', category: null,
          components: { commands: 1, skills: 0, agents: 0, mcpServers: 0, hooks: 0 }, tokenCost: null,
        }],
        marketplaces: [],
        installEnabled: true,
      }
    // Collection endpoints answer `[]`, never `{}`: the app `.map()`s and `.find()`s them.
    default: return []
  }
}

/**
 * Stand the seeded backend up on `page`. Call before `page.goto`.
 *
 * Also pins the two sources of run-to-run drift a pixel or a geometry
 * assertion would otherwise inherit: wall-clock time (`SEED_NOW`) and motion
 * (`prefers-reduced-motion`, which the app's CSS honours).
 */
export async function installSeededApi(page: Page, overrides: SeedOverrides = {}): Promise<void> {
  await page.clock.setFixedTime(SEED_NOW)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    return route.fulfill({ json: seededResponse(path, overrides) })
  })
  // Accept the socket, stay silent: no server frames, so no reconnect churn.
  await page.routeWebSocket('**/ws', (ws) => { ws.onMessage(() => {}) })
}
