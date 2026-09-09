/**
 * The Roadmap workspace the shell owns — `useRoadmapWorkspace` in `AppShell`.
 *
 * The subject is the GATE. `AppShellInner` calls the hook unconditionally (hooks
 * always are), so nothing about the code's shape stops `/pm/map` being polled
 * every 15 s on Sessions, Pipeline, Automations, Triage, Insights and Settings —
 * six sections that never render the Roadmap column — and on a wall display,
 * which has no `app.list` region at all. Only `enabled` stops it, and only a test
 * that watches the TRANSPORT can tell a disabled query from an enabled one: both
 * return `data: undefined` to the component.
 *
 * So the api CLIENT is faked and the real hooks run against it, the same
 * arrangement as `routes.test.tsx` and `features/settings/pageContract.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

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

  it.each(['/sessions', '/pipeline', '/automations', '/triage', '/insights', '/settings/general'])(
    'does not fetch the map on %s',
    async (path) => {
      renderApp({ path, mode: 'desktop' })
      await shellSettled()
      expect(askedForMap()).toBe(false)
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

  it('fetches the map on the aliased initiative page', async () => {
    // `/initiatives/:slug` is Roadmap by SECTION ALIAS, not by path prefix. The
    // gate reads `navDefForPath(...)?.id`, so an alias that stopped resolving
    // would silently drop the list column beside the initiative page.
    renderApp({ path: '/initiatives/views', mode: 'desktop' })
    await waitFor(() => expect(askedForMap()).toBe(true))
  })

  it('starts fetching only once the user navigates to Roadmap', async () => {
    const { router } = renderApp({ path: '/pipeline', mode: 'desktop' })
    await shellSettled()
    expect(askedForMap()).toBe(false)

    await router.navigate('/roadmap')

    await waitFor(() => expect(askedForMap()).toBe(true))
  })
})
