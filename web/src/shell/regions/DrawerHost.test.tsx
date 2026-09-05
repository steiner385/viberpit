// DrawerHost — the `app.drawer` region: ONE `Sheet` whose content is selected by
// `?drawer=`.
//
// The properties this file exists to pin:
//
//   • The drawer is URL state, and an *unrecognised* value is not a drawer. A
//     `?drawer=bogus` that opened an empty sheet would be a dead modal the user
//     can only escape by hitting Close — so "absent" is asserted as "the node is
//     not in the document", never as "the node is empty".
//
//   • Closing REPLACES the entry (umbrella §4.4: overlay params always replace).
//     That is asserted two ways — `historyAction`, and a real `go(-1)`: with a
//     push the back entry would still carry `?drawer=help`.
//
//   • The Navigation list is DERIVED from `NAV_DEFS` — the loop below is over
//     `NAV_DEFS` itself, so a section added there with no `help` string cannot
//     compile, and one added without a row in the sheet fails here. Its
//     predecessor, `HelpDrawer`'s hand-written `NAV_MAP`, listed five sections
//     when the app had eight (audit F1.3).
//
// Scope note on axe: jsdom applies no stylesheet, so the axe pass here covers
// roles, names and ARIA wiring ONLY. Its `color-contrast` rule always returns
// `incomplete` and `toHaveNoViolations()` inspects `violations` alone — a green
// assertion here is NOT evidence of adequate contrast. Contrast is checked
// separately by src/__tests__/contrast.test.ts against the tokens in index.css.
import type { ReactNode } from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { cleanup, render, screen, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { createMemoryRouter, RouterProvider, Outlet, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ViewportProvider } from '@/shell/useViewport'
import { NAV_DEFS } from '@/shell/navDefs'
import { SHORTCUTS, displayKeys } from '@/shell/shortcuts'
import { DRAWER_STORAGE_KEY, DRAWER_DEFAULT } from '@/shell/drawerWidth'
import { MEDIA, stubMatchMedia } from '@/test/matchMedia'

// AssistantSheet (rendered for `?drawer=assistant`) owns a real WebSocket
// attach/detach cycle via SessionRuntimeProvider — mocked here the same way
// `AssistantSheet.test.tsx` does, so this file can render the REAL DrawerHost →
// REAL AssistantSheet without a live socket.
vi.mock('@/api/assistant', () => ({
  useAssistantStatus: vi.fn(),
  useAssistantSession: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false, isError: false, error: null })),
  useResetAssistant: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useSetAssistantModel: vi.fn(() => ({
    mutateAsync: vi.fn(), isPending: false, isError: false, error: null,
  })),
}))
vi.mock('@/components/session/SessionRuntimeProvider', () => ({
  SessionRuntimeProvider: ({ children }: { children: ReactNode }) => <div data-testid="runtime">{children}</div>,
  useSessionRuntimeContext: vi.fn(),
}))

import { useAssistantStatus } from '@/api/assistant'
import { useSessionRuntimeContext } from '@/components/session/SessionRuntimeProvider'
import { DrawerHost } from './DrawerHost'
import { GROUPS } from './HelpSheet'

expect.extend(toHaveNoViolations)

/** A fully-formed (fake) SessionRuntime for the mocked `useSessionRuntimeContext` —
 *  see AssistantSheet.test.tsx, which this mirrors. */
function fakeRuntime() {
  return {
    sessionId: 'a1',
    state: {
      turns: [], openBlockId: null, status: { state: 'idle', count: 0 }, title: null, model: null,
      usage: null, contextUsage: null, replaying: false, earliestTurn: 0, totalTurns: 0,
      loadingOlder: false, error: null, queued: [], rotations: [], subAgents: {},
      latestSuggestions: [], condense: null, pendingSent: [], pendingSentSeq: 0, hadReset: false,
      orphanToolResults: {}, staleTurns: [], staleSubAgents: {},
    },
    mode: 'interactive',
    toolDetailMode: 'auto',
    thinkingCollapsed: true,
    connectionState: 'connected',
    reattach: vi.fn(),
    title: 'Assistant',
    goal: null,
    goalSource: null,
    account: '—',
    model: null,
    modelPending: false,
    liveModel: null,
    quota: null,
    contextPct: null,
    subAgents: {},
    hasOpenAction: false,
    hasOpenPermission: false,
    armedQuestionId: null,
    chatArmed: false,
    onDecision: vi.fn(),
    onAnswer: vi.fn(),
    onChat: vi.fn(),
    onPrompt: vi.fn(),
    onActivity: vi.fn(),
    onInterrupt: vi.fn(),
    onInterruptAndPrompt: vi.fn(() => true),
    onSetMode: vi.fn(),
    onCancelQueued: vi.fn(),
    commands: [],
    commandResult: null,
    onCommand: vi.fn(),
    onSetModel: vi.fn(),
    dismissCommandResult: vi.fn(),
    onLoadOlder: vi.fn(),
  }
}

/** Arms the assistant drawer's mocked hooks for the enabled+live-session state. */
function enableAssistant() {
  vi.mocked(useAssistantStatus).mockReturnValue(
    { data: { enabled: true, sessionId: 'a1', model: 'sonnet' }, isLoading: false } as never,
  )
  vi.mocked(useSessionRuntimeContext).mockReturnValue(fakeRuntime() as never)
}

// AssistantHeader (rendered inside AssistantSheet) reads the real `useModels()`
// (deliberately NOT mocked — the assistant's own status/session hooks above are
// the ones this file fakes), so the tree needs a real QueryClient. Given no
// mock, the query just fails against jsdom's absent network and renders the
// header's fallback label — nothing here asserts on it.
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Layout() {
  const { pathname, search } = useLocation()
  return (
    <QueryClientProvider client={queryClient}>
      <ViewportProvider kiosk={null}>
        <div data-testid="url">{pathname + search}</div>
        <DrawerHost />
        <Outlet />
      </ViewportProvider>
    </QueryClientProvider>
  )
}

function renderAt(entries: string[], index = entries.length - 1) {
  const router = createMemoryRouter(
    [
      {
        path: '/',
        element: <Layout />,
        children: [
          { path: 'sessions', element: <div>sessions page</div> },
          { path: 'archived', element: <div>archived page</div> },
        ],
      },
    ],
    { initialEntries: entries, initialIndex: index },
  )
  render(<RouterProvider router={router} />)
  return router
}

/** The region node itself — queried from the document because an overlay sheet
 *  portals out of the React container. */
const region = () => document.querySelector('[data-region="app.drawer"]')

/** The region, or a failure naming it — for the assertions that must have one. */
function regionEl(): HTMLElement {
  const el = region()
  if (!(el instanceof HTMLElement)) throw new Error('no [data-region="app.drawer"] in the document')
  return el
}

describe('DrawerHost', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  // `expectNoDrawer` asserts the PAGE is still there as well as the drawer being
  // gone, and that is not belt-and-braces. A `DrawerHost` that throws on an
  // unknown name satisfies "no dialog in the document" perfectly — React
  // Router's default ErrorBoundary swallows the throw and replaces the whole
  // route with an error page. That is how this exact assertion passed against a
  // deliberately broken build during review, and the failure mode it hides (a
  // stale `?drawer=` link white-screening the app) is far worse than the empty
  // sheet the test was written to prevent.
  function expectNoDrawer() {
    expect(region()).toBeNull()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByText('sessions page')).toBeInTheDocument()
  }

  it('is absent from the document when ?drawer= is unset', () => {
    renderAt(['/sessions'])
    expectNoDrawer()
  })

  it('renders nothing for an unrecognised ?drawer= value', () => {
    renderAt(['/sessions?drawer=bogus'])
    expectNoDrawer()
  })

  it('carries data-region="app.drawer" on the panel, exactly once', () => {
    renderAt(['/sessions?drawer=help'])
    expect(document.querySelectorAll('[data-region="app.drawer"]')).toHaveLength(1)
    expect(region()).toBe(screen.getByRole('dialog', { name: /help/i }))
  })

  it('?drawer=help renders the help sheet', () => {
    renderAt(['/sessions?drawer=help'])
    expect(screen.getByRole('dialog', { name: /help/i })).toBeInTheDocument()
  })

  it('lists every NAV_DEFS section, label and help text (drift guard)', () => {
    renderAt(['/sessions?drawer=help'])
    const dlg = screen.getByRole('dialog', { name: /help/i })
    for (const def of NAV_DEFS) {
      expect(within(dlg).getByText(def.label), `${def.id} label`).toBeInTheDocument()
      expect(within(dlg).getByText(def.help), `${def.id} help`).toBeInTheDocument()
    }
  })

  it('lists every registry shortcut description (drift guard)', () => {
    renderAt(['/sessions?drawer=help'])
    const dlg = screen.getByRole('dialog', { name: /help/i })
    for (const s of SHORTCUTS) {
      expect(within(dlg).getAllByText(s.description).length, s.id).toBeGreaterThan(0)
    }
  })

  /**
   * The description guard above catches a missing group only as a downstream
   * symptom — a row in an unlisted group has no section to render into, so its
   * description simply never appears. This asserts the actual invariant
   * directly: the set of groups `HelpSheet` renders a section for is exactly
   * the set of groups `SHORTCUTS` uses, computed from the registry itself
   * (never a hand-written list of group names, which is precisely the kind of
   * list that drifted before — see `HelpDrawer`'s `NAV_MAP` in this file's
   * header comment).
   */
  it('renders exactly the shortcut groups present in the registry (no group silently dropped)', () => {
    const registryGroups = new Set(SHORTCUTS.map((s) => s.group))
    const sheetGroups = new Set(GROUPS.map((g) => g.group))
    expect(sheetGroups).toEqual(registryGroups)
  })

  it('never renders "undefined" for a shortcut row', () => {
    renderAt(['/sessions?drawer=help'])
    const dlg = screen.getByRole('dialog', { name: /help/i })
    expect(dlg.textContent ?? '').not.toMatch(/undefined/)
  })

  // Migrated from `components/__tests__/HelpDrawer.test.tsx` (Task 18); the
  // component it tested was deleted in Task 17 and replaced by `HelpSheet`.
  it("renders each shortcut's KEYS, not only its description", () => {
    // The description loop above passes on a sheet that lists what every
    // shortcut DOES and never says which key does it. `HelpDrawer.test.tsx`
    // caught that with two literals (`c`, `a / Backspace`); this derives the
    // whole set from the registry, so a binding that renders no key fails here.
    //
    // Asserted through `displayKeys(s)`, not the `s.keys` literal: this suite
    // runs on jsdom's default (non-Apple) `navigator.platform`, where the two
    // happen to agree for every row, but `displayKeys` is the actual contract
    // the sheet renders through (`HelpSheet`'s `<Kbd keys={displayKeys(b)} />`)
    // — asserting the literal would keep passing even if that call were
    // swapped for `b.keys` on this platform, which the Apple-glyph test below
    // exists to catch on the platform where the two rows this PR shipped
    // (`app.help`, `app.assistant`) actually diverge.
    renderAt(['/sessions?drawer=help'])
    const dlg = screen.getByRole('dialog', { name: /help/i })
    for (const s of SHORTCUTS) {
      expect(within(dlg).getAllByText(displayKeys(s)).length, `${s.id} keys`).toBeGreaterThan(0)
    }
    // …and nothing still shows the pre-registry Ctrl bindings.
    expect(dlg.textContent ?? '').not.toMatch(/Ctrl\+X/)
  })

  /**
   * The Apple branch of `displayKeys()`, rendered through the real sheet.
   *
   * Until Task 5 no row in `SHORTCUTS` carried a `keysMac`, so every assertion
   * above — including the KEYS drift guard — exercised only the non-Apple side,
   * and `HelpSheet`'s `displayKeys(b)` would have passed just as well written as
   * `b.keys`. A Mac user must be shown `⌘/`, never `Ctrl+/`: telling them to
   * press a modifier their keyboard has in a different place is a shortcut they
   * will conclude is broken.
   *
   * jsdom reports `navigator.platform` as `''`, so it is redefined rather than
   * cast into place (R10), and restored so the non-Apple assertions elsewhere in
   * this file keep testing what they say they do.
   */
  it('shows the ⌘ glyphs on an Apple platform, and Ctrl elsewhere', () => {
    const original = window.navigator.platform
    const setPlatform = (value: string) =>
      Object.defineProperty(window.navigator, 'platform', { value, configurable: true })

    const withMac = SHORTCUTS.filter((s) => s.keysMac !== undefined)
    expect(withMac.map((s) => s.id), 'no row carries keysMac').not.toHaveLength(0)

    try {
      setPlatform('MacIntel')
      renderAt(['/sessions?drawer=help'])
      const mac = screen.getByRole('dialog', { name: /help/i })
      for (const s of withMac) {
        expect(within(mac).getAllByText(s.keysMac ?? '').length, `${s.id} on MacIntel`)
          .toBeGreaterThan(0)
        expect(within(mac).queryByText(s.keys), `${s.id} shows its Ctrl twin on a Mac`).toBeNull()
      }

      cleanup()

      setPlatform('Win32')
      renderAt(['/sessions?drawer=help'])
      const win = screen.getByRole('dialog', { name: /help/i })
      for (const s of withMac) {
        expect(within(win).getAllByText(s.keys).length, `${s.id} on Win32`).toBeGreaterThan(0)
        expect(within(win).queryByText(s.keysMac ?? ''), `${s.id} shows ⌘ on Windows`).toBeNull()
      }
    } finally {
      setPlatform(original)
    }
  })

  it('has Navigation and Tips sections', () => {
    renderAt(['/sessions?drawer=help'])
    const dlg = within(screen.getByRole('dialog', { name: /help/i }))
    expect(dlg.getByRole('heading', { name: /navigation/i })).toBeInTheDocument()
    expect(dlg.getByRole('heading', { name: /tips/i })).toBeInTheDocument()
  })

  it('Escape and a backdrop click each clear ?drawer=', async () => {
    const user = userEvent.setup()

    const escaped = renderAt(['/sessions?drawer=help'])
    await user.keyboard('{Escape}')
    expect(escaped.state.location.search).toBe('')
    expect(region()).toBeNull()

    cleanup()

    const clicked = renderAt(['/sessions?drawer=help'])
    await user.click(screen.getByTestId('sheet-backdrop'))
    expect(clicked.state.location.search).toBe('')
    expect(region()).toBeNull()
  })

  it('does not document Shift+Tab (D8 removed the binding and its handler)', () => {
    renderAt(['/sessions?drawer=help'])
    const dlg = screen.getByRole('dialog', { name: /help/i })
    expect(dlg.textContent ?? '').not.toMatch(/shift\s*\+\s*tab/i)
  })

  it('?drawer=assistant renders AssistantSheet', () => {
    enableAssistant()
    renderAt(['/sessions?drawer=assistant'])
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    expect(within(dlg).getByTestId('runtime')).toBeInTheDocument()
    expect(within(dlg).getByRole('textbox')).toBeInTheDocument()
    expect(within(dlg).getByText(/AI-generated/)).toBeInTheDocument()
  })

  it('?drawer=assistant shows a settings pointer, not a composer, when disabled', () => {
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: false, sessionId: null, model: 'sonnet' }, isLoading: false } as never,
    )
    renderAt(['/sessions?drawer=assistant'])
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    expect(within(dlg).getByText(/enable it in settings/i)).toBeInTheDocument()
    expect(within(dlg).queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('closing clears ?drawer= with replace, so history does not grow', async () => {
    const user = userEvent.setup()
    const router = renderAt(['/sessions', '/sessions?drawer=help'])
    await user.click(screen.getByRole('button', { name: /close/i }))

    expect(screen.getByTestId('url')).toHaveTextContent('/sessions')
    expect(region()).toBeNull()
    expect(router.state.historyAction).toBe('REPLACE')

    // The real proof: the entry the drawer was opened on was REPLACED, so going
    // back lands on the pre-drawer entry, not on `?drawer=help` again.
    await act(async () => {
      await router.navigate(-1)
    })
    expect(router.state.location.search).toBe('')
  })

  it('stays mounted, as the same node, across a route change', async () => {
    const router = renderAt(['/sessions?drawer=help'])
    const before = region()
    expect(before).not.toBeNull()
    await act(async () => {
      await router.navigate('/archived?drawer=help')
    })
    expect(screen.getByTestId('url')).toHaveTextContent('/archived?drawer=help')
    expect(region()).toBe(before)
  })

  it('is an overlay sheet below the push breakpoint', () => {
    renderAt(['/sessions?drawer=help'])
    expect(region()).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByTestId('sheet-backdrop')).toBeInTheDocument()
  })

  it('is a non-modal push column at the drawer-push breakpoint', () => {
    stubMatchMedia([MEDIA.tablet, MEDIA.desktop, MEDIA.drawerPush])
    renderAt(['/sessions?drawer=help'])
    expect(region()).not.toHaveAttribute('aria-modal')
    expect(screen.queryByTestId('sheet-backdrop')).not.toBeInTheDocument()
    expect(screen.getByRole('separator', { name: /resize drawer/i })).toBeInTheDocument()
  })

  it('renders at the persisted width and writes nudges back to ccswitch.drawerWidth', async () => {
    stubMatchMedia([MEDIA.tablet, MEDIA.desktop, MEDIA.drawerPush])
    localStorage.setItem(DRAWER_STORAGE_KEY, '520')
    const user = userEvent.setup()
    renderAt(['/sessions?drawer=help'])
    expect(region()).toHaveStyle({ width: '520px' })

    // The handle sits on the drawer's left edge, so ← widens it.
    const handle = screen.getByRole('separator', { name: /resize drawer/i })
    handle.focus()
    await user.keyboard('{ArrowLeft}')
    expect(region()).toHaveStyle({ width: '536px' })
    expect(localStorage.getItem(DRAWER_STORAGE_KEY)).toBe('536')
  })

  it('falls back to the default width when nothing is persisted', () => {
    renderAt(['/sessions?drawer=help'])
    expect(region()).toHaveStyle({ width: `${DRAWER_DEFAULT}px` })
  })

  // Scoped to the panel, not `document.body`: an overlay sheet portals out of
  // the React container, and the body here also holds this file's own routing
  // probe, which is not landmarked and is not the component under test.
  it('has no axe violations (help)', async () => {
    renderAt(['/sessions?drawer=help'])
    expect(await axe(regionEl())).toHaveNoViolations()
  })

  it('has no axe violations (assistant)', async () => {
    enableAssistant()
    renderAt(['/sessions?drawer=assistant'])
    expect(await axe(regionEl())).toHaveNoViolations()
  })

  /**
   * Push mode, which no axe pass rendered before: the two cases above are
   * overlays, which mount no resizer at all.
   *
   * The property is CONTAINMENT — the focusable `role="separator"` handle is
   * inside a landmark rather than a bare sibling of every landmark, the defect
   * Task 17 fixed for `ContextList`. It is asserted structurally here because
   * axe's `region` rule needs the whole document to judge it, and this file's
   * body also holds an un-landmarked routing probe; the document-level pass is
   * `AppShell.test.tsx`'s "has no axe violations with the drawer in push mode".
   */
  it('has no axe violations in push mode, and the resizer is inside the landmark', async () => {
    stubMatchMedia([MEDIA.tablet, MEDIA.desktop, MEDIA.drawerPush])
    renderAt(['/sessions?drawer=help'])

    const landmark = screen.getByRole('complementary', { name: 'Help & Shortcuts' })
    expect(landmark).toContainElement(screen.getByRole('separator', { name: /resize drawer/i }))
    expect(landmark).toContainElement(regionEl())
    expect(await axe(landmark)).toHaveNoViolations()
  })
})
