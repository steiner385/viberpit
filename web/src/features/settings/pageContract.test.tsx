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
import { screen, waitFor } from '@testing-library/react'

// Same arrangement as `SettingsPage.test.tsx`: the transport is faked and the
// real hooks run against it, so a page that starts asking for something new
// fails loudly rather than silently receiving `undefined`.
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  const { appSettings } = await import('@/test/factories')
  const { MAP } = await import('@/features/roadmap/__fixtures__/map')
  const { AUTOMATIONS } = await import('@/features/delivery/__fixtures__/automations')
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/fleet/hosts') return { hosts: [] }
        if (path === '/config') return { adoptionEnabled: false }
        if (path === '/settings') return appSettings()
        if (path === '/pm/map') return MAP
        // Delivery's two scopes: the automation list names the automation row
        // below, and `/pipeline/repos` is the CI scope's repo set. The per-id
        // `/automations/<id>/…` reads (actionable, events, sessions) fall
        // through to `[]` and must NOT be answered with the list.
        if (path === '/automations') return AUTOMATIONS
        if (path.startsWith('/automations/')) return []
        if (path === '/pipeline/repos') return { 'o/r': { repo: 'o/r', nodes: [] } }
        if (path.startsWith('/pipeline/insights/checks')) {
          return {
            repo: 'o/r', window: '7d',
            slowest: [], flakiest: [], regressions: [], runner_wait_health: [],
          }
        }
        if (path === '/pm/queue') return { entries: [], skipped: 0, generatedAt: 'now' }
        if (path === '/sdlc/triage') return { items: [] }
        // The Roadmap section's Inbox scope, in every scope: the list column
        // badges the queue depth, and the page finds the open proposal in it.
        if (path.startsWith('/pm/triage')) return []
        // The initiative page names itself from THIS payload, so the row below
        // asserts the fetched title rather than the slug in the URL.
        if (path.startsWith('/sdlc/initiatives/')) {
          return {
            slug: 'views', title: 'Use-case-first task views', stage: 'build', status: 'active',
            repo: 'cairnea/KinDash', origin: null, originRef: null, epicRepo: null, epicNumber: null,
            createdAt: null, updatedAt: null, artifacts: [], epic: null, projectSlug: 'kindash',
            primaryCapability: null, capabilities: [], buildSessions: [], nextAction: { type: 'unknown' },
          }
        }
        if (path.startsWith('/pm/leaves')) return { items: [] }
        // The specific `/pm/projects/…` shapes come BEFORE the generic one:
        // `startsWith('/pm/projects/')` would otherwise swallow them. `/deps`
        // is answered even though nothing reads it yet — `DependencyPanel`
        // dereferences `drift` unguarded, so the generic `Project` shape would
        // throw the moment a later task re-homes it under this page.
        if (path.startsWith('/pm/projects') && path.endsWith('/deps')) {
          return {
            project: null,
            edges: [],
            drift: {
              proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [],
            },
          }
        }
        if (path.startsWith('/pm/projects') && path.endsWith('/onboarding')) {
          return {
            state: 'live',
            action: { type: 'none' },
            counts: {
              repoCount: 0, backfillDoneCount: 0, featureCount: 0,
              openUnclassifiedCount: 0, pendingProposalCount: 0,
            },
            mapPr: null,
          }
        }
        if (path.startsWith('/pm/projects/')) {
          return {
            id: 'p', slug: 'kindash', name: 'KinDash', repos: [], edges: [], portfolioTags: [],
            description: null, status: null, onboardingState: 'live',
            createdAt: null, updatedAt: null, featureCount: 0, workItemCount: 0,
          }
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
  { nav: 'settings', path: '/settings/general',                    variant: 'split',    title: 'Settings' },
  { nav: 'roadmap',  path: '/roadmap',                             variant: 'standard', title: 'Roadmap' },
  { nav: 'roadmap',  path: '/roadmap/kindash',                     variant: 'standard', title: 'KinDash' },
  { nav: 'roadmap',  path: '/roadmap/kindash/capabilities/tasks',  variant: 'standard', title: 'Tasks' },
  { nav: 'roadmap',  path: '/roadmap?scope=inbox',                 variant: 'standard', title: 'Inbox' },
  { nav: 'roadmap',  path: '/initiatives/views',                   variant: 'standard', title: 'Use-case-first task views' },
  // Both Delivery welcome scopes keep the SECTION title: `?scope=ci` is a scope
  // of Delivery, not a page of its own. Only a detail route retitles.
  { nav: 'delivery', path: '/delivery',                            variant: 'standard', title: 'Delivery' },
  { nav: 'delivery', path: '/delivery?scope=ci',                   variant: 'standard', title: 'Delivery' },
  { nav: 'delivery', path: '/delivery/automations/pr-doctor',      variant: 'standard', title: 'PR doctor' },
  { nav: 'delivery', path: '/delivery/ci/o/r',                     variant: 'standard', title: 'o/r' },
  // The first Workspace-variant page (finish-line row 5). Kiosk-safe, so it also
  // gets a kiosk cell. No SSE frame ever arrives here (`EventSource` is stubbed
  // below), so what the row asserts is the loading state of the contract.
  { nav: 'pipeline', path: '/pipeline',                            variant: 'workspace', title: 'Pipeline' },
]

// jsdom has no EventSource; the Pipeline page opens one on mount.
class StubEventSource {
  onopen = null; onmessage = null; onerror = null
  addEventListener() {}
  close() {}
}

describe('page contract', () => {
  beforeEach(() => {
    localStorage.clear()
    resetDocumentTitle()
    vi.stubGlobal('EventSource', StubEventSource as unknown as typeof EventSource)
  })

  for (const page of PAGES) {
    // A page kiosk is not allowed on has no kiosk cell: `KioskPageGuard`
    // redirects the URL away, so the cell would assert the kiosk home page
    // instead of this one.
    for (const mode of LAYOUT_MODES.filter((m) => m !== 'kiosk' || NAV_BY_ID[page.nav].showInKiosk)) {
      it(`${page.nav} × ${mode}: one h1, document.title, variant, no horizontal scroll`, async () => {
        renderApp({ path: page.path, mode })

        // Two deviations from a plain `findAllByRole`, both about WHEN and WHERE
        // the heading is readable rather than about the contract itself:
        //
        // · by NAME — a page whose title comes from a query (Roadmap names the
        //   product or capability the URL points at) renders its `h1` at once
        //   with a slug placeholder, so waiting on the level alone resolves
        //   before the real title lands and the assertion reads the placeholder.
        // · `hidden: true` — on phone and tablet a section that owns a list
        //   column gives the single content band to the LIST, and `PageHost`
        //   marks the page `hidden` (§4.1). The page is mounted and correct; it
        //   is behind the list. Which region owns the band is the region
        //   matrix's claim, not this table's.
        // The wait itself carries the title assertion — `name` matches the
        // accessible name — so all that is left to assert is that there is
        // exactly ONE h1.
        await screen.findByRole('heading', { level: 1, name: page.title, hidden: true })
        expect(screen.getAllByRole('heading', { level: 1, hidden: true })).toHaveLength(1)
        await waitFor(() => expect(document.title).toMatch(new RegExp(`${page.title} — ViberPit$`)))
        expect(document.querySelector(`[data-page-variant="${page.variant}"]`)).not.toBeNull()

        const root = document.getElementById('root') ?? document.body
        expect(root.scrollWidth).toBeLessThanOrEqual(root.clientWidth)
      })
    }
  }
})
