/**
 * The Settings PAGE — the Split-variant page that replaced `SettingsView`'s
 * fourteen-tab strip (spec §6.1, umbrella D11).
 *
 * ## Why this mounts the real app
 *
 * Every claim here is about the page IN the shell: which layout mode drills, what
 * `document.title` reads, and where a legacy URL lands. `renderApp` mounts the
 * real `AppShell` around the real route table, so a redirect that only works
 * under a stand-in layout cannot pass.
 *
 * ## Why the API CLIENT is faked rather than `@/api/hooks`
 *
 * The brief said to copy `routes.test.tsx`'s `vi.mock('@/api/hooks', …)`; that
 * file has no such block — it fakes `@/api/client` and lets the real hooks run
 * against it. That is the stronger arrangement and the one copied here: the
 * hosted sections this page renders (`AccountsManager`, `FleetSection`,
 * `HooksSection`, `SkillsSection`) each pull their own hooks, and a hand-listed
 * hooks mock is a list that silently goes stale the moment a section adds one.
 * Faking the transport means every section asks for its data for real and gets a
 * fixed answer, so a section that starts reading something new fails loudly here
 * instead of receiving `undefined`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// The fixtures are imported INSIDE the factory: `vi.mock` is hoisted above every
// import, so a factory that closed over a module-scope binding would read it
// before `@/test/factories` had been evaluated.
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  const { account, appSettings } = await import('@/test/factories')
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/fleet/hosts') return { hosts: [] }
        if (path === '/config') return { adoptionEnabled: false }
        if (path === '/hooks') return { events: [] }
        if (path === '/library/skills') return { skills: [] }
        if (path === '/library/plugins') return { plugins: [], marketplaces: [] }
        if (path === '/library/commands') return { commands: [] }
        if (path === '/library/mcp') return { servers: [] }
        if (path === '/settings') return appSettings()
        // Two accounts needing re-auth, so the in-Settings badge (C20) has a
        // count to announce on every render rather than only in its own test.
        if (path === '/accounts') {
          return [
            account({ name: 'work', status: 'needs-reauth' }),
            account({ name: 'personal', status: 'needs-relogin' }),
          ]
        }
        return []
      }),
      post: vi.fn(async () => ({})),
      put: vi.fn(async () => ({})),
      patch: vi.fn(async () => ({})),
      del: vi.fn(async () => undefined),
    },
  }
})

// The only genuinely external edge of the session runtime `PageHost` mounts.
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
import { MEDIA } from '@/test/matchMedia'
import { resetDocumentTitle } from '@/shell/documentTitle'
import { LAST_SECTION_KEY } from './lastSection'
import { SETTINGS_DEFS } from './settingsDefs'

describe('SettingsPage', () => {
  beforeEach(() => {
    localStorage.clear()
    resetDocumentTitle()
  })

  it('desktop: h1 Settings, grouped nav with aria-current, split variant', async () => {
    renderApp({ path: '/settings/sessions', mode: 'desktop' })

    expect(await screen.findByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Settings sections' })).toBeInTheDocument()
    // NavLink sets aria-current itself; the nav is derived from settingsDefs, so
    // this also pins "the routed section is the highlighted one".
    expect(screen.getByRole('link', { name: /Defaults/, current: 'page' })).toBeInTheDocument()
    expect(document.querySelector('[data-page-variant="split"]')).not.toBeNull()
    // The h1 stays "Settings" (one page, one name); the section qualifies the
    // TITLE only, so a tab strip of open Settings tabs is distinguishable.
    expect(document.title).toBe('Defaults · Settings — ViberPit')
  })

  it('desktop: the migrated Defaults section renders its own h2', async () => {
    // An `ownsChassis` section is rendered bare (no `HostedSection` wrapper), so the
    // chassis h2 it mounts itself is the only one on the page — which is what
    // makes this a check that the section, not the page, owns its heading.
    renderApp({ path: '/settings/sessions', mode: 'desktop' })

    // Waited on the CONTROL, not the heading: the section now renders its chassis
    // (heading included) while `/settings` is still in flight, so a heading query
    // resolves against the loading state and the form is not there yet.
    expect(await screen.findByLabelText('Default tool approvals')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Defaults' })).toBeInTheDocument()
    const section = screen.getByRole('region', { name: 'Defaults' })
    expect(within(section).getByLabelText('Default tool approvals')).toBeInTheDocument()
  })

  it('desktop: the section body owns the vertical scroll', async () => {
    // `PageHost` is a clipped flex column, so the page must own an
    // `overflow-y-auto` region or a long section overflows with nowhere to
    // scroll. `SettingsView`'s wrapper used to do this; `PageContent` does now.
    renderApp({ path: '/settings/fleet', mode: 'desktop' })
    const content = await screen.findByTestId('page-content')
    expect(content.className).toContain('overflow-y-auto')
  })

  it('desktop: bare /settings redirects to the last visited section, else general', async () => {
    const a = renderApp({ path: '/settings', mode: 'desktop' })
    await waitFor(() => expect(a.path()).toBe('/settings/general'))
    a.unmount()

    localStorage.setItem(LAST_SECTION_KEY, 'fleet')
    const b = renderApp({ path: '/settings', mode: 'desktop' })
    await waitFor(() => expect(b.path()).toBe('/settings/fleet'))
  })

  it('phone: /settings is the grouped list; a section has Back to Settings', async () => {
    const r = renderApp({ path: '/settings', mode: 'phone' })

    expect(await screen.findByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument()
    // The list IS the page on phone (iOS idiom, C18): no section is open, so
    // nothing has drilled in and there is no section heading yet.
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull()

    await userEvent.click(screen.getByRole('link', { name: /Hosts/ }))
    await waitFor(() => expect(r.path()).toBe('/settings/fleet'))
    expect(screen.getByRole('heading', { level: 2, name: 'Hosts' })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('link', { name: 'Back to Settings' }))
    await waitFor(() => expect(r.path()).toBe('/settings'))
  })

  it('tablet portrait drills like phone; tablet landscape shows the nav', async () => {
    const p = renderApp({ path: '/settings', mode: 'tablet', extraQueries: [MEDIA.portrait] })
    expect(await screen.findByRole('list', { name: 'Settings sections' })).toBeInTheDocument()
    expect(p.path()).toBe('/settings')
    p.unmount()

    const l = renderApp({ path: '/settings', mode: 'tablet' })
    await waitFor(() => expect(l.path()).toBe('/settings/general'))
    expect(screen.getByRole('navigation', { name: 'Settings sections' })).toBeInTheDocument()
  })

  it('unknown section → general; /settings/automation is a real section; /automation → it', async () => {
    const a = renderApp({ path: '/settings/nope', mode: 'desktop' })
    await waitFor(() => expect(a.path()).toBe('/settings/general'))
    a.unmount()

    // R10 reversed DEC-10: `/automations` MONITORS automations and has no
    // enable switch, mode select, config form or schedule editor — those exist
    // only in this section, so the URL must keep resolving to it (until PR 6).
    const b = renderApp({ path: '/settings/automation', mode: 'desktop' })
    expect(await screen.findByRole('region', { name: 'Automations' })).toBeInTheDocument()
    expect(b.path()).toBe('/settings/automation')
    b.unmount()

    const c = renderApp({ path: '/automation', mode: 'desktop' })
    await waitFor(() => expect(c.path()).toBe('/settings/automation'))
  })

  it('keeps legacy links: /settings?section=…, /accounts, /library?section=skills, ?dialog=settings', async () => {
    for (const [from, to] of [
      ['/settings?section=hooks', '/settings/hooks'],
      ['/accounts', '/settings/accounts'],
      ['/library?section=skills', '/settings/skills'],
      ['/sessions?dialog=settings', '/settings/general'],
      // A stale link can carry a BLANK section. These two assert the LANDING
      // only. They cannot pin `settingsSectionPath`'s trim guard, and it would
      // be a lie to say they do: with the trim removed, `'  '` produces
      // `/settings/%20%20`, which `SettingsPage` then rejects as an unknown
      // section and redirects to General anyway — same landing, one extra
      // history entry, a nonsense URL in the bar in between. The guard itself is
      // unit-tested in `shell/navDefs.test.ts` (`settingsSectionPath`), which is
      // the only place the difference is visible.
      ['/settings?section=%20%20', '/settings/general'],
      ['/settings?section=', '/settings/general'],
    ] as const) {
      // Per iteration, not per test: a preceding row lands on a section and
      // `writeLastSection` remembers it, which is exactly what the two blank
      // rows fall through to. Without this, `?section=` would assert the row
      // above it rather than the default.
      localStorage.clear()
      const r = renderApp({ path: from, mode: 'desktop' })
      await waitFor(() => expect(r.path()).toBe(to))
      r.unmount()
    }
  })

  it('phone: a blank legacy ?section= is the list, not a redirect', async () => {
    // The blank rows above are desktop-only on purpose. On phone nothing named a
    // section, so `/settings` is legitimately the grouped list — asserting a
    // redirect to General here would encode "the phone skips its own list" as
    // the requirement.
    const r = renderApp({ path: '/settings?section=', mode: 'phone' })
    expect(await screen.findByRole('list', { name: 'Settings sections' })).toBeInTheDocument()
    expect(r.path()).toBe('/settings')
  })

  it('every section is one chassis region; ownsChassis sections own exactly one h2', async () => {
    // The table version of "the page wraps what it must and nothing else"
    // (DEC-12): an `ownsChassis` section mounts its own chassis and so must show
    // exactly ONE level-2 heading inside its region — a section that keeps a
    // literal heading after migrating would nest two — and a hosted section must
    // still be wrapped, i.e. have a region named by its nav label at all.
    for (const def of SETTINGS_DEFS) {
      const r = renderApp({ path: `/settings/${def.id}`, mode: 'desktop' })
      const region = (await screen.findAllByRole('region', { name: def.label }))[0]
      const h2s = within(region).getAllByRole('heading', { level: 2 })
      expect(h2s[0]).toHaveTextContent(def.label)
      if (def.id === 'accounts') {
        // The one exception, and it is temporary: `AccountsManager` carries its own
        // `<h2>Accounts</h2>` inside the chassis one until PR 5c migrates Accounts
        // (spec §11). Every other hosted section is held to exactly one, so a body
        // that grows a second heading is caught here rather than at 5c.
        expect(h2s.length).toBeGreaterThanOrEqual(1)
      } else {
        expect(h2s).toHaveLength(1)
      }
      r.unmount()
    }
  }, 60_000)

  it('remembers the visited section and shows the accounts attention count', async () => {
    renderApp({ path: '/settings/accounts', mode: 'desktop' })

    // The chassis heading, named through the section landmark rather than by
    // `getByRole('heading', …)`: the UNMIGRATED `AccountsManager` still carries
    // its own legacy `<h2>Accounts</h2>` inside the chassis one, so a bare
    // role+name query matches two nodes. PR 5c's migrated Accounts section drops
    // the inner heading and this can go back to a plain heading query.
    const section = await screen.findByRole('region', { name: 'Accounts' })
    expect(within(section).getAllByRole('heading', { level: 2 })[0]).toHaveTextContent('Accounts')

    expect(localStorage.getItem(LAST_SECTION_KEY)).toBe('accounts')
    expect(await screen.findByRole('link', { name: /Accounts \(2\)/ })).toBeInTheDocument()
  })
})
