// PromptDock — the `app.dock` region.
//
// ## What this file has to prove, and why it is mounted the hard way
//
// The dock's whole job is a branch: `SessionComposer` when a session is open,
// `DispatchBar` when none is. The two halves are not interchangeable —
// `SessionComposer` reads `useSessionRuntimeContext()`, which THROWS outside a
// provider. So the branch is not a cosmetic choice; getting it wrong takes the
// route down.
//
// That is why nothing here stubs `useSessionRuntimeContext`. The dock is mounted
// through the REAL `PageHost`, exactly as `AppShell` will mount it, so the
// provider is genuinely present on `/sessions/:id` and genuinely absent on
// `/sessions`. A pass-through mock of the context would make every assertion
// below pass no matter which branch the dock chose — Task 11 refused that same
// shortcut for the same reason.
//
// Only the two real external edges are faked: `@/api/client`'s `api` (jsdom has
// no daemon, and the POST body is what pins DEC-2) and `@/ws/client`'s socket
// (the pattern `SessionRuntimeProvider.test.tsx` and `PageHost.test.tsx` use).
// Every hook — `useCreateSession`, `useFleetHosts`, `useSessionListState`,
// `useSessionRuntime` — is the real one.
//
// ## DEC-2
//
// The fleet-host Runner picker was phone-only: `Dashboard` fetched the hosts and
// passed them to `DispatchBar`; `DesktopShell.handleDispatch` had no `host` in
// its parameter type at all. One dock means one `DispatchBar`, so desktop gains
// it. Two tests pin it end to end — the dropdown is rendered from the ENABLED
// hosts, and the chosen host reaches the `POST /sessions` body — so it cannot
// quietly regress to phone-only.
//
// ## Scope note on axe
//
// jsdom applies no stylesheet, so the axe pass here covers roles, names and ARIA
// wiring ONLY. Its `color-contrast` rule always returns `incomplete` and
// `toHaveNoViolations()` inspects `violations` alone — a green assertion is NOT
// evidence of adequate contrast. Contrast is checked separately by
// src/__tests__/contrast.test.ts against the tokens in index.css.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { createMemoryRouter, RouterProvider, useLocation, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

expect.extend(toHaveNoViolations)

import type { ModelOption, Session } from '@/api/types'
import type { GithubRepo } from '@/api/hooks'
import type { FleetHost } from '@/api/fleet'
import { account, fleetHost, session as makeSession } from '@/test/factories'

// ── typed factories (R10: real values, never `as unknown as T`) ───────────────
// `session`, `account` and `fleetHost` come from @/test/factories; this suite's
// only local default is the session id it navigates to.

const session = (over: Partial<Session> = {}): Session => makeSession({ id: 'a', ...over })

function repo(over: Partial<GithubRepo> = {}): GithubRepo {
  return {
    nameWithOwner: 'acme/api',
    name: 'api',
    owner: 'acme',
    isPrivate: false,
    url: 'https://github.com/acme/api',
    defaultBranch: 'main',
    pushedAt: null,
    cloned: true,
    ...over,
  }
}

function model(over: Partial<ModelOption> = {}): ModelOption {
  return { id: 'opus', label: 'Opus', ...over }
}

// ── the fake daemon ──────────────────────────────────────────────────────────
//
// Reassigned per test; read at request time, never at mock-factory time.

let SESSIONS: Session[] = []
let HOSTS: FleetHost[] = []
let REPOS: GithubRepo[] = []
let MODELS: ModelOption[] = []
/** Resolves the next `POST /sessions`; a test may swap in a rejection. */
let POST_SESSIONS: (body: unknown) => Promise<unknown> = async () => session({ id: 'new-1' })

const posted: { path: string; body: unknown }[] = []

const fakeGet = vi.fn(async (path: string): Promise<unknown> => {
  if (path === '/sessions') return SESSIONS
  if (path === '/fleet/hosts') return { hosts: HOSTS }
  if (path === '/github/repos') return REPOS
  if (path === '/models') return MODELS
  if (path === '/accounts') return [account()]
  return {}
})

const fakePost = vi.fn(async (path: string, body?: unknown): Promise<unknown> => {
  posted.push({ path, body })
  if (path === '/sessions') return POST_SESSIONS(body)
  return {}
})

// `ApiRequestError` stays REAL: the 409 branch below narrows on `err.status`,
// and a stubbed error class would let a broken narrowing pass.
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      get: (path: string) => fakeGet(path),
      post: (path: string, body?: unknown) => fakePost(path, body),
      put: vi.fn(),
      patch: vi.fn(),
      del: vi.fn(),
    },
  }
})

// The websocket is the only genuinely external edge of the REAL runtime provider.
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

import { ApiRequestError } from '@/api/client'
import { useSessionListState } from '@/features/sessions/useSessionListState'
import { PageHost } from './PageHost'
import { PromptDock } from './PromptDock'

// ── harness ──────────────────────────────────────────────────────────────────

const noop = () => {}

interface HarnessOpts {
  onEnterList?: () => void
  onShowHelp?: () => void
  onAllLimited?: (earliestReset: string | null) => void
}

/** Mounts the dock the way `AppShell` will: through `PageHost`'s `dock` slot,
 *  with the list state owned above the region (shared with `ContextList`). */
function Layout(opts: HarnessOpts) {
  const list = useSessionListState(SESSIONS)
  return (
    <div>
      <Where />
      <GoTo to="/sessions/b" />
      <GoTo to="/sessions" />
      {/* `DispatchBar`'s aria-controls points at the list column's listbox, which
          lives in `ContextList` — a different region, deliberately not mounted
          here. Standing it in keeps the axe pass about the dock rather than
          about the harness. */}
      <div id="session-listbox" />
      <PageHost
        dock={
          <PromptDock
            list={list}
            accounts={[account()]}
            onEnterList={opts.onEnterList ?? noop}
            onShowHelp={opts.onShowHelp ?? noop}
            onAllLimited={opts.onAllLimited ?? noop}
          />
        }
      />
    </div>
  )
}

/** Navigates for real — a route change, not a re-render of the tree. */
function GoTo({ to }: { to: string }) {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate(to)}>{`go ${to}`}</button>
}

/** Surfaces the live location so navigation is asserted on the router, not on a spy. */
function Where() {
  const { pathname, state } = useLocation()
  const prompt =
    typeof state === 'object' && state !== null && 'initialPrompt' in state
      ? String((state as { initialPrompt?: unknown }).initialPrompt)
      : ''
  return <div data-testid="where">{`${pathname}|${prompt}`}</div>
}

function Page({ name }: { name: string }) {
  return <div data-testid={`page-${name}`}>{name}</div>
}

function renderAt(pathname: string, opts: HarnessOpts = {}) {
  const router = createMemoryRouter(
    [
      {
        element: <Layout {...opts} />,
        children: [
          { path: '/sessions', element: <Page name="list" /> },
          { path: '/sessions/:id', element: <Page name="session" /> },
          { path: '/pipeline', element: <Page name="pipeline" /> },
        ],
      },
    ],
    { initialEntries: [pathname] },
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

const dockNode = () => document.querySelector('[data-region="app.dock"]')
/** `DispatchBar`'s prompt — the new-session half. */
const dispatchPrompt = () => screen.queryByLabelText('New session prompt')
/** `Composer`'s prompt — the live-session half. */
const composerPrompt = () => screen.queryByLabelText('Message')

beforeEach(() => {
  SESSIONS = [session({ id: 'a', name: 'First' }), session({ id: 'b', name: 'Second' })]
  HOSTS = [fleetHost({ name: 'edge' }), fleetHost({ name: 'rocky' })]
  REPOS = [repo()]
  MODELS = [model()]
  POST_SESSIONS = async () => session({ id: 'new-1' })
  posted.length = 0
  localStorage.clear()
  vi.clearAllMocks()
})

// ── the region marker ────────────────────────────────────────────────────────

describe('PromptDock — the app.dock region', () => {
  it('marks itself as app.dock, exactly once, in both branches', async () => {
    const { unmount } = renderAt('/sessions')
    expect(document.querySelectorAll('[data-region="app.dock"]')).toHaveLength(1)
    unmount()

    renderAt('/sessions/a')
    await screen.findByLabelText('Message')
    expect(document.querySelectorAll('[data-region="app.dock"]')).toHaveLength(1)
  })

  it('sits inside app.main, so the composer shares the transcript runtime', async () => {
    renderAt('/sessions/a')
    await screen.findByLabelText('Message')
    expect(document.querySelector('[data-region="app.main"]')).toContainElement(
      dockNode() as HTMLElement,
    )
  })

  it('has no axe violations (roles/names only — jsdom has no stylesheet)', async () => {
    const { container } = renderAt('/sessions')
    expect(await axe(container)).toHaveNoViolations()
  })
})

// ── the branch ───────────────────────────────────────────────────────────────

describe('PromptDock — DispatchBar vs SessionComposer', () => {
  it('renders the DispatchBar, and no composer, when no session is open', () => {
    renderAt('/sessions')
    expect(dispatchPrompt()).toBeInTheDocument()
    expect(composerPrompt()).not.toBeInTheDocument()
  })

  it('renders the SessionComposer, and no DispatchBar, when a session is open', async () => {
    renderAt('/sessions/a')
    // The composer only renders at all because `useSessionRuntimeContext()` found
    // the provider `PageHost` mounts. Nothing here stubs that hook, so this
    // assertion fails loudly — with the provider's own throw — if the dock ever
    // picks the composer branch on a route that has no runtime.
    expect(await screen.findByLabelText('Message')).toBeInTheDocument()
    expect(dispatchPrompt()).not.toBeInTheDocument()
  })

  it('swaps branches in both directions as the route changes', async () => {
    const user = userEvent.setup()
    renderAt('/sessions')
    expect(dispatchPrompt()).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'go /sessions/b' }))
    expect(await screen.findByLabelText('Message')).toBeInTheDocument()
    expect(dispatchPrompt()).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'go /sessions' }))
    expect(await screen.findByLabelText('New session prompt')).toBeInTheDocument()
    expect(composerPrompt()).not.toBeInTheDocument()
  })
})

// ── DEC-2: the fleet-host picker is no longer phone-only ─────────────────────

describe('PromptDock — the Runner picker (DEC-2)', () => {
  it('offers the enabled fleet hosts, wherever the dock is mounted', async () => {
    HOSTS = [fleetHost({ name: 'edge' }), fleetHost({ name: 'rocky' })]
    renderAt('/sessions')
    const runner = await screen.findByLabelText('Runner')
    expect(
      Array.from(runner.querySelectorAll('option')).map((o) => o.textContent),
    ).toEqual(['Auto', 'edge', 'rocky'])
  })

  it('leaves out hosts that are disabled', async () => {
    HOSTS = [fleetHost({ name: 'edge' }), fleetHost({ name: 'rocky', enabled: false })]
    renderAt('/sessions')
    const runner = await screen.findByLabelText('Runner')
    const names = Array.from(runner.querySelectorAll('option')).map((o) => o.textContent)
    expect(names).toContain('edge')
    expect(names).not.toContain('rocky')
  })

  it('shows no Runner dropdown when the fleet has no enabled hosts', async () => {
    HOSTS = []
    renderAt('/sessions')
    await screen.findByLabelText('Repository')
    expect(screen.queryByLabelText('Runner')).not.toBeInTheDocument()
  })

  it('threads the chosen host into POST /sessions', async () => {
    const user = userEvent.setup()
    renderAt('/sessions')
    const runner = await screen.findByLabelText('Runner')

    await user.selectOptions(runner, 'rocky')
    await user.type(screen.getByLabelText('New session prompt'), 'ship it')
    await user.keyboard('{Enter}')

    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0].path).toBe('/sessions')
    expect(posted[0].body).toMatchObject({ initialPrompt: 'ship it', host: 'rocky' })
  })

  it('sends no host when the runner is left on Auto', async () => {
    const user = userEvent.setup()
    renderAt('/sessions')
    await screen.findByLabelText('Runner')

    await user.type(screen.getByLabelText('New session prompt'), 'ship it')
    await user.keyboard('{Enter}')

    await waitFor(() => expect(posted).toHaveLength(1))
    expect((posted[0].body as { host?: unknown }).host).toBeUndefined()
  })
})

// ── dispatch ─────────────────────────────────────────────────────────────────

describe('PromptDock — dispatch', () => {
  it('opens the created session and seeds the prompt through router state', async () => {
    const user = userEvent.setup()
    POST_SESSIONS = async () => session({ id: 'new-1' })
    renderAt('/sessions')

    await user.type(await screen.findByLabelText('New session prompt'), 'fix the bug')
    await user.keyboard('{Enter}')

    await waitFor(() =>
      expect(screen.getByTestId('where')).toHaveTextContent('/sessions/new-1|fix the bug'),
    )
  })

  it("carries the repo's default branch, the way both shells did", async () => {
    const user = userEvent.setup()
    REPOS = [repo({ nameWithOwner: 'acme/api', defaultBranch: 'trunk' })]
    renderAt('/sessions')

    await screen.findByRole('option', { name: 'acme/api' })
    await user.selectOptions(screen.getByLabelText('Repository'), 'acme/api')
    await user.type(screen.getByLabelText('New session prompt'), 'go')
    await user.keyboard('{Enter}')

    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0].body).toMatchObject({ repo: 'acme/api', branch: 'trunk' })
  })

  it('hands a 409 "all accounts limited" reset time to the shell banner', async () => {
    const user = userEvent.setup()
    const onAllLimited = vi.fn()
    POST_SESSIONS = async () => {
      throw new ApiRequestError(409, { earliestReset: '2026-09-03T18:00:00Z' })
    }
    renderAt('/sessions', { onAllLimited })

    await user.type(await screen.findByLabelText('New session prompt'), 'go')
    await user.keyboard('{Enter}')

    // DesktopShell had this branch and Dashboard did not — the union keeps it,
    // so the phone layout gains the banner instead of silently swallowing 409.
    await waitFor(() => expect(onAllLimited).toHaveBeenCalledWith('2026-09-03T18:00:00Z'))
  })

  it('keeps the draft when creation fails, so the user can retry', async () => {
    const user = userEvent.setup()
    POST_SESSIONS = async () => {
      throw new ApiRequestError(500, {})
    }
    renderAt('/sessions')

    const prompt = await screen.findByLabelText('New session prompt')
    await user.type(prompt, 'retry me')
    await user.keyboard('{Enter}')

    await waitFor(() => expect(posted).toHaveLength(1))
    expect(prompt).toHaveValue('retry me')
  })
})

// ── the shared list cursor ───────────────────────────────────────────────────

describe('PromptDock — one cursor, shared with the list', () => {
  it('drives the shared highlight from the prompt (aria-activedescendant)', async () => {
    const user = userEvent.setup()
    renderAt('/sessions')
    const prompt = await screen.findByLabelText('New session prompt')

    await user.click(prompt)
    await user.keyboard('{ArrowDown}')

    // The id comes from `useSessionListState`'s nav handle — the same handle
    // `ContextList` renders its selection from. A second cursor would leave this
    // undefined no matter how the list looked.
    expect(prompt).toHaveAttribute('aria-activedescendant', 'row-a')
  })

  it('asks the shell to enter list-focus mode on Tab, and seeds the cursor', async () => {
    const user = userEvent.setup()
    const onEnterList = vi.fn()
    renderAt('/sessions', { onEnterList })
    const prompt = await screen.findByLabelText('New session prompt')

    await user.click(prompt)
    await user.tab()

    expect(onEnterList).toHaveBeenCalled()
    expect(prompt).toHaveAttribute('aria-activedescendant', 'row-a')
  })

  it('opens the highlighted session on Enter with an empty prompt', async () => {
    const user = userEvent.setup()
    renderAt('/sessions')
    const prompt = await screen.findByLabelText('New session prompt')

    await user.click(prompt)
    await user.keyboard('{ArrowDown}{ArrowDown}')
    await user.keyboard('{Enter}')

    expect(screen.getByTestId('where')).toHaveTextContent('/sessions/b|')
    expect(posted).toHaveLength(0)
  })

  it('opens a session by ordinal on Alt+digit', async () => {
    const user = userEvent.setup()
    renderAt('/sessions')
    const prompt = await screen.findByLabelText('New session prompt')

    await user.click(prompt)
    await user.keyboard('{Alt>}2{/Alt}')

    expect(screen.getByTestId('where')).toHaveTextContent('/sessions/b|')
  })

  it('asks the shell for help on ? with an empty prompt', async () => {
    const user = userEvent.setup()
    const onShowHelp = vi.fn()
    renderAt('/sessions', { onShowHelp })

    await user.click(await screen.findByLabelText('New session prompt'))
    await user.keyboard('?')

    expect(onShowHelp).toHaveBeenCalled()
  })
})

// ── safe-area padding ────────────────────────────────────────────────────────

describe('PromptDock — the mobile safe area', () => {
  // The dock does NOT add its own inset. `DispatchBar` and `Composer` each
  // already carry `paddingBottom: calc(env(safe-area-inset-bottom) + …)` on the
  // surface they paint, and a second one on the region would stack: ~34px of
  // inset plus 0.5rem, twice, under the input on an edge-to-edge phone.
  //
  // The dock still depends on that padding existing, so it is asserted here —
  // on the element that actually paints the bottom edge in each branch. Strip it
  // from either child and this fails, in the region that would look broken.
  /** The single element the dock renders — whichever branch it chose. */
  const surface = () => (dockNode() as HTMLElement).firstElementChild as HTMLElement

  it('clears the gesture bar under the DispatchBar', async () => {
    renderAt('/sessions')
    await screen.findByLabelText('New session prompt')
    expect(surface().style.paddingBottom).toContain('env(safe-area-inset-bottom)')
  })

  it('clears the gesture bar under the session composer', async () => {
    renderAt('/sessions/a')
    await screen.findByLabelText('Message')
    expect(surface().style.paddingBottom).toContain('env(safe-area-inset-bottom)')
  })

  it('adds no second inset of its own — the region must not double it', () => {
    renderAt('/sessions')
    expect((dockNode() as HTMLElement).style.paddingBottom).toBe('')
  })
})

// ── drafts across a session switch (a deliberate decision, not an accident) ───

describe('PromptDock — drafts across a :id change', () => {
  it('discards an unsent composer draft when the open session changes', async () => {
    // DELIBERATE, and unchanged from what ships today. `app.main` is keyed on the
    // session id so each session gets a clean reducer, which remounts the dock
    // with it; `DesktopShell` keyed the whole shell and did exactly the same.
    // Preserving drafts needs a per-session store OUTSIDE the keyed subtree —
    // out of scope here, and noted in the task report.
    const user = userEvent.setup()
    renderAt('/sessions/a')
    const first = await screen.findByLabelText('Message')
    await user.type(first, 'half-written thought')
    expect(first).toHaveValue('half-written thought')
    const before = dockNode()

    await user.click(screen.getByRole('button', { name: 'go /sessions/b' }))

    // A NEW node, not merely a present one: the keyed remount is the mechanism,
    // and `toBeInTheDocument()` would pass either way and prove nothing.
    expect(dockNode()).not.toBe(before)
    expect(await screen.findByLabelText('Message')).toHaveValue('')
  })
})
