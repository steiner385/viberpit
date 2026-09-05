/**
 * The PAGE CONTRACT table (DEC-5).
 *
 * Sub-spec C §2.3 says every migrated page has exactly one `h1`, syncs it to
 * `document.title`, declares its layout variant, and never scrolls sideways — in
 * every layout mode the page is reachable in. That is a property of the page
 * SET, not of any one page, so it is asserted as a table: a page migrates by
 * gaining a row here, and the row is what makes the four claims true of it.
 *
 * It lives beside Settings because Settings is the first (and, today, only) row.
 * Later PRs add rows rather than files.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'

// Same arrangement as `SettingsPage.test.tsx`: the transport is faked and the
// real hooks run against it, so a page that starts asking for something new
// fails loudly rather than silently receiving `undefined`.
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  const { appSettings } = await import('@/test/factories')
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/fleet/hosts') return { hosts: [] }
        if (path === '/config') return { adoptionEnabled: false }
        if (path === '/settings') return appSettings()
        return []
      }),
      post: vi.fn(async () => ({})),
      put: vi.fn(async () => ({})),
      patch: vi.fn(async () => ({})),
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
import { resetDocumentTitle } from '@/shell/documentTitle'
import { LAYOUT_MODES } from '@/shell/useViewport'
import { NAV_BY_ID, type NavId } from '@/shell/navDefs'

interface PageContractRow {
  nav: NavId
  path: string
  variant: 'standard' | 'split' | 'reader' | 'workspace'
  title: string
}

/** Pages that conform to sub-spec C §2.3. Add a row when a page migrates. */
const PAGES: readonly PageContractRow[] = [
  { nav: 'settings', path: '/settings/general', variant: 'split', title: 'Settings' },
]

describe('page contract', () => {
  beforeEach(() => {
    localStorage.clear()
    resetDocumentTitle()
  })

  for (const page of PAGES) {
    // A page kiosk is not allowed on has no kiosk cell: `KioskPageGuard`
    // redirects the URL away, so the cell would assert the kiosk home page
    // instead of this one.
    for (const mode of LAYOUT_MODES.filter((m) => m !== 'kiosk' || NAV_BY_ID[page.nav].showInKiosk)) {
      it(`${page.nav} × ${mode}: one h1, document.title, variant, no horizontal scroll`, async () => {
        renderApp({ path: page.path, mode })

        const h1s = await screen.findAllByRole('heading', { level: 1 })
        expect(h1s).toHaveLength(1)
        expect(h1s[0]).toHaveTextContent(page.title)
        expect(document.title).toMatch(new RegExp(`${page.title} — ccswitch$`))
        expect(document.querySelector(`[data-page-variant="${page.variant}"]`)).not.toBeNull()

        const root = document.getElementById('root') ?? document.body
        expect(root.scrollWidth).toBeLessThanOrEqual(root.clientWidth)
      })
    }
  }
})
