/**
 * The Roadmap workspace the shell owns — `useRoadmapWorkspace` in `AppShell`.
 *
 * The subject is the GATE. `AppShellInner` calls the hook unconditionally (hooks
 * always are), so nothing about the code's shape stops `/pm/map` being polled
 * every 15 s on Sessions, Pipeline, Automations, Insights and Settings —
 * five sections that never render the Roadmap column — and on a wall display,
 * which has no `app.list` region at all. Only `enabled` stops it, and only a test
 * that watches the TRANSPORT can tell a disabled query from an enabled one: both
 * return `data: undefined` to the component.
 *
 * So the api CLIENT is faked and the real hooks run against it, the same
 * arrangement as `routes.test.tsx` and `features/settings/pageContract.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  const { MAP } = await import('@/features/roadmap/__fixtures__/map')
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/fleet/hosts') return { hosts: [] }
        if (path === '/config') return { adoptionEnabled: false }
        if (path === '/pm/map') return MAP
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

import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { ROADMAP } from '@/features/products/vocab'

const get = vi.mocked(api.get)
const askedForMap = () => get.mock.calls.some(([path]) => path === '/pm/map')
const triageCalls = () => get.mock.calls.map(([path]) => path).filter((p) => p.startsWith('/pm/triage'))

/** The shell has mounted and made its first round of requests. `/config` is
 *  asked for on every route by `useSessionsWorkspace`, so it is the settle
 *  point — waiting on it means a MISSING `/pm/map` below is a decision the shell
 *  made, not a request that had not been issued yet. */
async function shellSettled() {
  await waitFor(() => expect(get.mock.calls.some(([p]) => p === '/config')).toBe(true))
}

describe('the shell only funds the roadmap map where the column renders', () => {
  beforeEach(() => {
    localStorage.clear()
    get.mockClear()
  })

  it.each(['/sessions', '/pipeline', '/automations', '/insights', '/settings/general'])(
    'does not fetch the map on %s',
    async (path) => {
      renderApp({ path, mode: 'desktop' })
      await shellSettled()
      expect(askedForMap()).toBe(false)
    },
  )

  // The queue is fetched TWICE over, for two different readers, and only one of
  // them is gated. Finish-line row 3 put the pending-proposal count on the
  // Roadmap NAV badge, which every section renders, so one unscoped
  // `/pm/triage` poll everywhere is the ruling rather than a leak — it is also
  // what makes the nav badge and the Inbox scope badge agree, since both read
  // the same unscoped key. What stays gated is the COLUMN's query: the
  // `?projectSlug=` scoped fetch its rows come from only goes out where the
  // column renders.
  it.each(['/sessions', '/pipeline', '/automations', '/insights', '/settings/general'])(
    'funds only the nav badge\'s unscoped triage poll on %s',
    async (path) => {
      renderApp({ path, mode: 'desktop' })
      await shellSettled()
      await waitFor(() => expect(triageCalls()).toContain('/pm/triage'))
      expect(triageCalls().filter((p) => p !== '/pm/triage')).toEqual([])
    },
  )

  it('fetches the map on the Roadmap section, and renders the column from it', async () => {
    renderApp({ path: '/roadmap', mode: 'desktop' })
    await waitFor(() => expect(askedForMap()).toBe(true))
    // Not just "the request went out": the column the request funds is on screen.
    expect(await screen.findByRole('tree', { name: ROADMAP.map })).toBeInTheDocument()
  })

  it('fetches the map on a roadmap DETAIL route too — the column stays beside it', async () => {
    renderApp({ path: '/roadmap/kindash/capabilities/tasks', mode: 'desktop' })
    await waitFor(() => expect(askedForMap()).toBe(true))
  })

  it('marks the open product selected in the column — the URL IS the selection', async () => {
    // End to end through the real hooks: `roadmapOpenItem(params)` →
    // `keyForOpenItem` → the row's `aria-selected`. A resolver that stopped
    // matching would leave a tree where nothing is ever the current row.
    renderApp({ path: '/roadmap/kindash', mode: 'desktop' })
    const row = await screen.findByTestId('row-product:kindash')
    expect(row).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('row-product:viberpit')).toHaveAttribute('aria-selected', 'false')
  })

  it('fetches the map on the aliased initiative page', async () => {
    // `/initiatives/:slug` is Roadmap by SECTION ALIAS, not by path prefix. The
    // gate reads `navDefForPath(...)?.id`, so an alias that stopped resolving
    // would silently drop the list column beside the initiative page.
    renderApp({ path: '/initiatives/views', mode: 'desktop' })
    await waitFor(() => expect(askedForMap()).toBe(true))
  })

  it('scopes the Inbox to ?product= and mounts that scope in the column', async () => {
    // The `?product=` chip is the query key AND the mutations' cache key, so a
    // resolver that stopped reading it would leave the optimistic accept
    // patching a list the rows did not come from.
    renderApp({ path: '/roadmap?scope=inbox&product=kindash', mode: 'desktop' })
    await waitFor(() =>
      expect(get.mock.calls.some(([p]) => p === '/pm/triage?projectSlug=kindash')).toBe(true))
    // Scoped to the column's scope switch: the page's breadcrumb offers a
    // second "Inbox" link (back to every product) whenever the scope is
    // narrowed, and it is the SEGMENT that has to mark the current scope.
    const segment = await screen.findByRole('navigation', { name: 'Roadmap scope' })
    const link = within(segment).getByRole('link', { name: new RegExp(`^${ROADMAP.inbox}`) })
    expect(link).toHaveAttribute('aria-current', 'page')
  })

  it('starts fetching only once the user navigates to Roadmap', async () => {
    const { router } = renderApp({ path: '/pipeline', mode: 'desktop' })
    await shellSettled()
    expect(askedForMap()).toBe(false)

    await router.navigate('/roadmap')

    await waitFor(() => expect(askedForMap()).toBe(true))
  })
})
