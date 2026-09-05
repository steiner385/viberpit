/**
 * The Automations SECTION of Settings — the only place an automation can be
 * CONFIGURED.
 *
 * ## Why this file still exists, and what it now asserts (R10)
 *
 * It used to mount `SettingsView` and assert `role="tab"` ids, and when that
 * component was deleted the natural reading was that the file went with it —
 * DEC-10 said Automations had moved to the top-level `/automations` page.
 *
 * That reading was wrong, and this file is the guard against making it again:
 * `automations/AutomationsTab.tsx` is a MONITOR. It renders events, spawned
 * sessions, actionable items and manual dispatch, and it has no enable switch,
 * no mode select, no config form and no schedule editor. Those three controls
 * exist ONLY in `AutomationView` → `AutomationCard` → `ScheduleEditor`, which
 * this section hosts. So the assertions below are no longer about a tab strip:
 * they name the controls, so deleting the section again fails here rather than
 * in a user's hands. PR 6 folds them into the Automations page and retires the
 * section; until then, this is the room and this is its only door.
 *
 * It renders the real page at `/settings/automation` (not the component in
 * isolation) because "reachable" is half the claim.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const PATCHED: unknown[] = []

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  const { appSettings } = await import('@/test/factories')
  const automation = {
    id: 'pr-doctor',
    name: 'PR Doctor',
    description: 'watches CI',
    enabled: false,
    mode: 'propose',
    modes: ['off', 'auto', 'propose'],
    status: { text: 'idle', level: 'idle', counts: null },
    // A schema is what makes the card expandable, which is where the config
    // form and the schedule editor live.
    configSchema: [
      { key: 'draft_only', label: 'Draft PRs only', type: 'bool', default: false, help: '', choices: null },
    ],
    config: { draft_only: true },
    schedule: null,
    lastActiveAt: null,
    activeSessions: 0,
  }
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/automations') return [automation]
        if (path === '/fleet/hosts') return { hosts: [] }
        if (path === '/config') return { adoptionEnabled: false }
        if (path === '/settings') return appSettings()
        if (path === '/github/repos') return []
        return []
      }),
      post: vi.fn(async () => ({})),
      put: vi.fn(async () => ({})),
      patch: vi.fn(async (path: string, body: unknown) => {
        PATCHED.push({ path, body })
        return { ...automation, ...(body as object) }
      }),
      del: vi.fn(async () => undefined),
    },
  }
})

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), cancelQueued: vi.fn(), command: vi.fn(),
      activity: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

import { renderApp } from '@/test/renderApp'

describe('Automations section (Settings)', () => {
  beforeEach(() => {
    PATCHED.length = 0
    localStorage.clear()
  })

  it('is reachable at /settings/automation and is listed in the Settings nav', async () => {
    const r = renderApp({ path: '/settings/automation', mode: 'desktop' })

    expect(await screen.findByRole('region', { name: 'Automations' })).toBeInTheDocument()
    expect(r.path()).toBe('/settings/automation')
    const nav = within(screen.getByRole('navigation', { name: 'Settings sections' }))
    expect(nav.getByRole('link', { name: 'Automations', current: 'page' })).toBeInTheDocument()
  })

  it('renders the enable switch and the mode select — the controls the Automations PAGE lacks', async () => {
    renderApp({ path: '/settings/automation', mode: 'desktop' })

    const toggle = await screen.findByRole('switch', { name: 'PR Doctor enabled' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByRole('combobox', { name: 'Mode' })).toHaveValue('propose')

    // The switch reaches the server: the section is wired, not decorative.
    // `mode` is left alone because 'propose' is already an active mode.
    await userEvent.click(toggle)
    expect(PATCHED).toEqual([
      { path: '/automations/pr-doctor', body: { enabled: true } },
    ])
  })

  it('expands to the config form and the schedule editor', async () => {
    renderApp({ path: '/settings/automation', mode: 'desktop' })

    await userEvent.click(await screen.findByRole('button', { name: 'Expand' }))

    // The config form, built from `configSchema`…
    expect(screen.getByLabelText('Draft PRs only')).toBeInTheDocument()
    // …and `ScheduleEditor`, the only schedule UI in the app. Named by its own
    // control rather than by the "Schedule" heading, which the panel and the
    // editor both render.
    expect(screen.getByRole('combobox', { name: 'Schedule preset' })).toBeInTheDocument()
  })
})
