import type { ReactNode } from 'react'
import { act, fireEvent, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

vi.mock('@/api/assistant', () => ({
  useAssistantStatus: vi.fn(),
  useAssistantNotices: vi.fn(() => ({ data: undefined })),
  useAssistantNoticeToasts: vi.fn(),
  useAssistantSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null })),
  useResetAssistant: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSetAssistantModel: vi.fn(() => ({
    mutate: vi.fn(), isPending: false, isError: false, error: null,
  })),
  useAssistantHistory: vi.fn(() => ({ data: [], isLoading: false })),
  useResumeAssistantSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isError: false })),
  useAssistantFeedback: vi.fn(() => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false })),
}))

// `toast` is spied (a dropped /btw says why, D-12); the rest of sonner stays real.
vi.mock('sonner', async (importOriginal) => {
  const actual = await importOriginal<typeof import('sonner')>()
  return { ...actual, toast: Object.assign(vi.fn(), actual.toast) }
})

// The real SessionRuntimeProvider owns a live WebSocket attach/detach cycle
// (useSessionRuntime) — well outside what this test needs to prove (states +
// wiring). Mocked as a dumb wrapper, alongside the context hook its descendants
// read, so `AssistantSheetBody` gets a fully-formed (fake) SessionRuntime without
// a real socket.
// The mocked provider still publishes WHICH session it is for (`useMockRuntimeSessionId`),
// so a test can hand the page's dock and the drawer the same fake runtime under their
// own session ids, as the real providers would (`runtimeFor`).
vi.mock('@/features/sessions/SessionRuntimeProvider', async () => {
  const { createContext, useContext } = await import('react')
  const Sid = createContext('')
  return {
    SessionRuntimeProvider: ({ children, sessionId }: { children: ReactNode; sessionId: string }) => (
      <Sid.Provider value={sessionId}><div data-testid="runtime">{children}</div></Sid.Provider>
    ),
    useSessionRuntimeContext: vi.fn(),
    useMockRuntimeSessionId: () => useContext(Sid),
  }
})

// The session page itself (transcript, rail, header) is not under test here — only the
// dock composer beside it and the drawer. A stub keeps the fake runtime minimal.
vi.mock('@/features/sessions/MainPaneSession', () => ({
  MainPaneSession: () => <div data-testid="session-page-stub" />,
}))

import { useAssistantSession, useAssistantStatus } from '@/api/assistant'
import { ApiRequestError } from '@/api/client'
import * as runtimeModule from '@/features/sessions/SessionRuntimeProvider'
import { useSessionRuntimeContext } from '@/features/sessions/SessionRuntimeProvider'
import { toast } from 'sonner'
// `renderApp` (web/src/test/renderApp.tsx) mounts the real shell at a path in a
// layout mode; opening `?drawer=assistant` on desktop renders the sheet through
// DrawerHost — the same route the user takes, so the test also proves the
// DrawerHost wiring (Task 2.3's DrawerHost.test.tsx covers the closing behavior).
import { renderApp } from '@/test/renderApp'

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
    armedQuestionId: null,
    chatArmed: false,
    onDecision: vi.fn(),
    onAnswer: vi.fn(),
    onChat: vi.fn(),
    onPrompt: vi.fn(),
    onPlainPrompt: vi.fn(() => 'sent'),
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

describe('AssistantSheet', () => {
  it('disabled → placeholder with settings pointer, no composer', () => {
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: false, sessionId: null, model: 'sonnet' }, isLoading: false } as never,
    )
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    // Scoped to the drawer: desktop mode also renders the sessions page's own
    // "New session" prompt textarea, which is unrelated to the assistant composer.
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    expect(within(dlg).getByText(/enable it in settings/i)).toBeInTheDocument()
    expect(within(dlg).queryByRole('textbox')).toBeNull()
  })

  it('enabled with a session id → mounts the runtime, starter chips, disclosure footer', () => {
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: true, sessionId: 'a1', model: 'sonnet' }, isLoading: false } as never,
    )
    vi.mocked(useSessionRuntimeContext).mockReturnValue(fakeRuntime() as never)
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    expect(screen.getByTestId('runtime')).toBeInTheDocument()
    expect(screen.getByText('What needs my attention?')).toBeInTheDocument()
    expect(screen.getByText(/AI-generated/)).toBeInTheDocument()
  })

  it('surfaces a session-level error frame inside the drawer', () => {
    // 2026-09-14: the edge answered a prompt with SESSION_NOT_FOUND and the drawer
    // showed nothing — only SessionBody rendered `state.error`, so the failure was
    // dropped on the floor and the bubble spun on "waking session…" indefinitely.
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: true, sessionId: 'a1', model: 'sonnet' }, isLoading: false } as never,
    )
    const rt = fakeRuntime()
    rt.state.error = { code: 'SESSION_NOT_FOUND', message: "Session 'a1' not found", resetsAt: null } as never
    vi.mocked(useSessionRuntimeContext).mockReturnValue(rt as never)
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    expect(within(dlg).getByRole('alert')).toHaveTextContent(/not found/i)
  })

  it('pins the permission card for an open gated tool call and routes the decision', () => {
    // 2026-09-16: the assistant raised `dispatch_backlog` (DESTRUCTIVE → always a card)
    // three times; the daemon pended each one and re-emitted it on attach, but the
    // drawer mounted no PendingPermissionPanel and Turn filters permission blocks out
    // of the transcript, so nothing rendered and the operator's next message
    // steer-denied every card ("The user sent a message instead of answering").
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: true, sessionId: 'a1', model: 'sonnet' }, isLoading: false } as never,
    )
    const rt = fakeRuntime()
    rt.state.turns = [{
      id: 't1', role: 'assistant',
      blocks: [{
        kind: 'permission', id: 'p1', permId: 'p1', tool: 'mcp__viberpit__dispatch_backlog',
        toolKind: 'app_action', input: { target: 'o/r#1' }, plan: false, alwaysAllowed: false, items: null,
      }],
    }] as never
    vi.mocked(useSessionRuntimeContext).mockReturnValue(rt as never)
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    const panel = within(dlg).getByTestId('pending-permission-panel')
    fireEvent.click(within(panel).getByRole('button', { name: /^allow/i }))
    expect(rt.onDecision).toHaveBeenCalledWith('p1', 'allow')
  })

  it('pins a live question so the assistant can be answered in place', () => {
    // Same blind spot for AskUserQuestion: Turn hides live questions in favour of the
    // pinned panel, which only SessionBody rendered.
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: true, sessionId: 'a1', model: 'sonnet' }, isLoading: false } as never,
    )
    const rt = fakeRuntime()
    rt.state.turns = [{
      id: 't1', role: 'assistant',
      blocks: [{
        kind: 'question', id: 'q1', questionId: 'q1', header: 'Scope', question: 'Which repo?',
        options: [{ label: 'viberpit' }, { label: 'kindash' }], multiSelect: false, live: true,
      }],
    }] as never
    vi.mocked(useSessionRuntimeContext).mockReturnValue(rt as never)
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    expect(within(dlg).getByRole('region', { name: /pending question/i })).toBeInTheDocument()
  })

  it('renders follow-up suggestion chips after an answer and sends one on tap (spec v2 §10.3)', () => {
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: true, sessionId: 'a1', model: 'sonnet' }, isLoading: false } as never,
    )
    const rt = fakeRuntime()
    rt.state.turns = [{ id: 't1', role: 'assistant', blocks: [{ kind: 'text', id: 'b1', text: 'Two sessions need you.' }] }] as never
    rt.state.latestSuggestions = ['Which one is oldest?', 'Open the first one'] as never
    vi.mocked(useSessionRuntimeContext).mockReturnValue(rt as never)
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    // Starters are gone once the thread has content; the follow-ups take their place.
    expect(within(dlg).queryByText('What needs my attention?')).toBeNull()
    fireEvent.click(within(dlg).getByRole('button', { name: 'Which one is oldest?' }))
    // Every drawer send carries the drawer's context — here, just the route (D-12).
    expect(rt.onPrompt).toHaveBeenCalledWith('Which one is oldest?', [], { route: '/sessions' }, 'suggested')
  })

  it.each(['desktop', 'phone'] as const)(
    '%s: the transcript is the drawer\'s scroll box, so it sticks to the bottom and offers Scroll to bottom',
    async (mode) => {
      // The drawer used to wrap TranscriptView in its own overflow-y-auto div: the
      // transcript grew to full height inside it, the wrapper scrolled instead, and
      // replies landed below the fold with no auto-follow and no button.
      vi.mocked(useAssistantStatus).mockReturnValue(
        { data: { enabled: true, sessionId: 'a1', model: 'sonnet' }, isLoading: false } as never,
      )
      const rt = fakeRuntime()
      rt.state.turns = [{ id: 't1', role: 'assistant', blocks: [{ kind: 'text', id: 'b1', text: 'Two sessions need you.' }] }] as never
      vi.mocked(useSessionRuntimeContext).mockReturnValue(rt as never)
      renderApp({ path: '/sessions?drawer=assistant', mode })
      const dlg = screen.getByRole('dialog', { name: /assistant/i })
      const scroller = within(dlg).getByTestId('transcript-scroll')
      // Nothing between the transcript and the Sheet's own body may scroll.
      const sheetBody = dlg.lastElementChild
      for (let n = scroller.parentElement; n && n !== sheetBody; n = n.parentElement) {
        expect(n.className).not.toMatch(/overflow-(y-)?(auto|scroll)/)
      }

      // Scrolling up in the drawer's transcript raises the shared button; it re-sticks.
      Object.defineProperty(scroller, 'scrollHeight', { value: 1000, configurable: true })
      Object.defineProperty(scroller, 'clientHeight', { value: 400, configurable: true })
      Object.defineProperty(scroller, 'scrollTop', { value: 600, writable: true, configurable: true })
      await act(() => new Promise<void>((r) => requestAnimationFrame(() => r())))
      scroller.scrollTop = 100
      fireEvent.scroll(scroller)
      const btn = within(dlg).getByRole('button', { name: 'Scroll to bottom' })
      fireEvent.click(btn)
      expect(scroller.scrollTop).toBe(1000)
      expect(within(dlg).queryByRole('button', { name: 'Scroll to bottom' })).toBeNull()
    },
  )

  it('shows no follow-up chips while a turn is running', () => {
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: true, sessionId: 'a1', model: 'sonnet' }, isLoading: false } as never,
    )
    const rt = fakeRuntime()
    rt.state.turns = [{ id: 't1', role: 'assistant', blocks: [{ kind: 'text', id: 'b1', text: 'Checking…' }] }] as never
    rt.state.latestSuggestions = ['Which one is oldest?'] as never
    rt.state.status = { state: 'working', count: 0 } as never
    vi.mocked(useSessionRuntimeContext).mockReturnValue(rt as never)
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    expect(within(dlg).queryByRole('button', { name: 'Which one is oldest?' })).toBeNull()
  })

  it('@ opens a mention menu over the sessions list and inserts a link on Enter (spec v2 §10.2)', async () => {
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: true, sessionId: 'a1', model: 'sonnet' }, isLoading: false } as never,
    )
    vi.mocked(useSessionRuntimeContext).mockReturnValue(fakeRuntime() as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const mentioned: Session = {
      id: 'sess-42', name: 'Fix the login bug', accountName: 'a',
      engine: 'claude_code', engineSessionId: null,
      status: 'idle', createdAt: '2026-06-21T00:00:00Z', updatedAt: '2026-06-21T00:00:00Z',
      prs: [],
    }
    qc.setQueryData(QK.sessions, [mentioned])
    const user = userEvent.setup()
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop', queryClient: qc })
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    const box = within(dlg).getByRole('textbox', { name: /message/i })
    await user.type(box, 'help with @fi')
    const listbox = within(dlg).getByRole('listbox', { name: /session mentions/i })
    expect(within(listbox).getByText('Fix the login bug')).toBeInTheDocument()
    await user.keyboard('{Enter}')
    expect(box).toHaveValue('help with [Fix the login bug](/sessions/sess-42) ')
  })

  // --- D-12: /btw and the scope indicator ------------------------------------------

  const BTW = { name: 'btw', description: '', argumentHint: '<question>', kind: 'client', source: 'app' }
  const OPEN: Session = {
    id: 's1', name: 'Fix the flaky login test', accountName: 'a',
    engine: 'claude_code', engineSessionId: null,
    status: 'in_progress', createdAt: '2026-06-21T00:00:00Z', updatedAt: '2026-06-21T00:00:00Z',
    prs: [],
  }
  const OTHER: Session = { ...OPEN, id: 's2', name: 'Ship the release notes', status: 'idle' }
  const CTX1 = { route: '/sessions/s1', sessionId: 's1' }
  function seeded() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(QK.sessions, [OPEN, OTHER])
    return qc
  }
  // One fake runtime, seen under each provider's own session id (the dock's `s1`, the
  // drawer's `a1`): reads go through to `rt`, so reassigning `rt.state` reaches both.
  function withBtw() {
    const rt = fakeRuntime()
    rt.commands = [BTW] as never
    const useSid = (runtimeModule as unknown as { useMockRuntimeSessionId: () => string }).useMockRuntimeSessionId
    const views = new Map<string, typeof rt>()
    const runtimeFor = (sid: string) => {
      if (!views.has(sid)) {
        views.set(sid, new Proxy(rt, { get: (t, k) => (k === 'sessionId' ? sid : t[k as keyof typeof t]) }))
      }
      return views.get(sid)!
    }
    vi.mocked(useSessionRuntimeContext).mockImplementation(() => runtimeFor(useSid()) as never)
    return rt
  }
  const enabled = (sessionId: string | null) => vi.mocked(useAssistantStatus).mockReturnValue(
    { data: { enabled: true, sessionId, model: 'sonnet' }, isLoading: false } as never,
  )
  const dock = () => within(document.querySelector<HTMLElement>('[data-region="app.dock"]')!)
    .getByRole('textbox', { name: /message/i })
  const drawer = () => screen.queryByRole('dialog', { name: /assistant/i })
  // Remount the sheet: open Help over the (sticky) Assistant, then drop it — the
  // Assistant comes back as a fresh mount. That is also how the sheet re-reads the
  // (mocked) assistant status: its drawer element is static, so a navigation alone
  // never re-renders it.
  const remount = async (router: { navigate: (to: string) => Promise<void> }, path = '/sessions/s1') => {
    await act(() => router.navigate(`${path}?drawer=help`))
    expect(drawer()).toBeNull()
    await act(() => router.navigate(path))
    expect(drawer()).not.toBeNull()
  }

  it('inside a session the header says what the side chat is about, with its status dot', () => {
    enabled('a1')
    withBtw()
    renderApp({ path: '/sessions/s1?drawer=assistant', mode: 'desktop', queryClient: seeded() })
    const about = within(drawer()!).getByTestId('assistant-scope')
    expect(about).toHaveTextContent('About: Fix the flaky login test')
    expect(within(about).getByTestId('session-status-dot').className).toMatch(/bg-success/)
  })

  it('outside a session the header names nothing', () => {
    enabled('a1')
    withBtw()
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop', queryClient: seeded() })
    expect(within(drawer()!).queryByTestId('assistant-scope')).toBeNull()
    expect(within(drawer()!).queryByText(/^About:/)).toBeNull()
  })

  it('/btw alone opens the drawer and sends nothing', async () => {
    enabled('a1')
    const rt = withBtw()
    const user = userEvent.setup()
    renderApp({ path: '/sessions/s1', mode: 'desktop', queryClient: seeded() })
    expect(drawer()).toBeNull()
    // Enter on the open slash menu picks `/btw ` (it takes an argument); Enter again sends it bare.
    await user.type(dock(), '/btw{Enter}')
    expect(dock()).toHaveValue('/btw ')
    await user.type(dock(), '{Enter}')
    expect(drawer()).not.toBeNull()
    expect(rt.onPrompt).not.toHaveBeenCalled()
    expect(rt.onPlainPrompt).not.toHaveBeenCalled()
    expect(rt.onCommand).not.toHaveBeenCalled()
    expect(dock()).toHaveValue('')
  })

  it('/btw <question> with the assistant session already there opens the drawer and sends it once, scoped', async () => {
    enabled('a1')
    const rt = withBtw()
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/s1', mode: 'desktop', queryClient: seeded() })
    await user.type(dock(), '/btw why is it failing{Enter}')
    expect(drawer()).not.toBeNull()
    expect(rt.onPlainPrompt).toHaveBeenCalledTimes(1)
    expect(rt.onPlainPrompt).toHaveBeenCalledWith('why is it failing', [], CTX1)
    expect(rt.onPrompt).not.toHaveBeenCalled()
    expect(rt.onCommand).not.toHaveBeenCalled()
    // The composer the user typed in is cleared, as for any client command.
    expect(dock()).toHaveValue('')
    // Re-rendering (a navigation) and remounting the sheet never resend.
    await act(() => app.router.navigate('/sessions/s1?drawer=assistant'))
    await remount(app.router)
    expect(rt.onPlainPrompt).toHaveBeenCalledTimes(1)
  })

  it('/btw <question> while the assistant session is still being created sends it once it exists (Review Focus 3)', async () => {
    enabled(null)
    const rt = withBtw()
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/s1', mode: 'desktop', queryClient: seeded() })
    await user.type(dock(), '/btw why is it failing{Enter}')
    expect(drawer()).not.toBeNull()
    expect(rt.onPlainPrompt).not.toHaveBeenCalled()
    // The create lands: the sheet now has a session (re-read on the remount).
    enabled('a1')
    await remount(app.router)
    expect(rt.onPlainPrompt).toHaveBeenCalledTimes(1)
    expect(rt.onPlainPrompt).toHaveBeenCalledWith('why is it failing', [], CTX1)
    await remount(app.router)
    expect(rt.onPlainPrompt).toHaveBeenCalledTimes(1)
  })

  it('waits for the replay to settle before sending', async () => {
    enabled('a1')
    const rt = withBtw()
    rt.state.replaying = true
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/s1', mode: 'desktop', queryClient: seeded() })
    await user.type(dock(), '/btw why{Enter}')
    expect(rt.onPlainPrompt).not.toHaveBeenCalled()
    rt.state = { ...rt.state, replaying: false }
    await act(() => app.router.navigate('/sessions/s1?drawer=assistant'))
    expect(rt.onPlainPrompt).toHaveBeenCalledTimes(1)
  })

  it('with the assistant switched off in Settings, /btw says so and sends nothing', async () => {
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: false, sessionId: null, model: 'sonnet' }, isLoading: false } as never,
    )
    const rt = withBtw()
    const user = userEvent.setup()
    renderApp({ path: '/sessions/s1', mode: 'desktop', queryClient: seeded() })
    await user.type(dock(), '/btw why{Enter}')
    expect(toast).toHaveBeenCalledWith('The Assistant is off — turn it on in Settings')
    expect(drawer()).toBeNull()
    expect(rt.onPrompt).not.toHaveBeenCalled()
    expect(rt.onCommand).not.toHaveBeenCalled()
  })

  it('a question parked when the assistant is then switched off is dropped, never sent later', async () => {
    enabled(null)
    const rt = withBtw()
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/s1', mode: 'desktop', queryClient: seeded() })
    await user.type(dock(), '/btw why{Enter}')
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: false, sessionId: null, model: 'sonnet' }, isLoading: false } as never,
    )
    await remount(app.router)
    expect(within(drawer()!).getByText(/enable it in settings/i)).toBeInTheDocument()
    enabled('a1')
    await remount(app.router)
    expect(within(drawer()!).getByTestId('runtime')).toBeInTheDocument()
    expect(rt.onPlainPrompt).not.toHaveBeenCalled()
  })

  it('⌘; still just opens the drawer — no send', () => {
    enabled('a1')
    const rt = withBtw()
    renderApp({ path: '/sessions/s1', mode: 'desktop', queryClient: seeded() })
    fireEvent.keyDown(window, { key: ';', ctrlKey: true })
    expect(drawer()).not.toBeNull()
    expect(rt.onPrompt).not.toHaveBeenCalled()
  })


  // --- D-12 fix round 1: the header never claims a scope the server isn't enforcing ---

  it('while the scope line shows, every drawer send names the session', async () => {
    enabled('a1')
    const rt = withBtw()
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/s1?drawer=assistant', mode: 'desktop', queryClient: seeded() })
    const dlg = () => within(drawer()!)
    expect(dlg().getByTestId('assistant-scope')).toBeInTheDocument()

    // Starter chip.
    fireEvent.click(dlg().getByRole('button', { name: 'Why is this session stuck?' }))
    expect(rt.onPrompt).toHaveBeenLastCalledWith('Why is this session stuck?', [], CTX1, 'suggested')
    // The composer.
    await user.type(dlg().getByRole('textbox', { name: /message/i }), 'hello{Enter}')
    expect(rt.onPrompt).toHaveBeenLastCalledWith('hello', [], CTX1)
    // A follow-up chip.
    rt.state = { ...rt.state,
      turns: [{ id: 't1', role: 'assistant', blocks: [{ kind: 'text', id: 'b1', text: 'Done.' }] }] as never,
      latestSuggestions: ['What next?'] as never }
    await act(() => app.router.navigate('/sessions/s1?drawer=assistant&r=1'))
    fireEvent.click(dlg().getByRole('button', { name: 'What next?' }))
    expect(rt.onPrompt).toHaveBeenLastCalledWith('What next?', [], CTX1, 'suggested')
    // Send now, mid-turn.
    rt.state = { ...rt.state, status: { state: 'in_progress', count: 0 } }
    await act(() => app.router.navigate('/sessions/s1?drawer=assistant&r=2'))
    await user.type(dlg().getByRole('textbox', { name: /message/i }), 'stop that')
    await user.click(dlg().getByRole('button', { name: /send now/i }))
    expect(rt.onInterruptAndPrompt).toHaveBeenLastCalledWith('stop that', [], undefined, CTX1)
    // A parked /btw.
    await user.type(dock(), '/btw and this?{Enter}')
    expect(rt.onPlainPrompt).toHaveBeenLastCalledWith('and this?', [], CTX1)
  })

  // The header reads the server's scope off the transcript: the context of the newest
  // STARTED user turn (the server emits it when the turn starts, and replays it). What
  // the client sent is not evidence — a queued send, a send mid-turn, or one taken as
  // an answer never moves it.
  const asked = (id: string, sessionId: string) => ({
    id, role: 'user', blocks: [{ kind: 'text', id: `${id}:t`, text: id }],
    context: { route: `/sessions/${sessionId}`, sessionId },
  })
  const answered = (id: string) => ({ id, role: 'assistant', blocks: [{ kind: 'text', id: `${id}:a`, text: 'ok' }] })
  const header = () => within(drawer()!).getByTestId('assistant-scope')

  it('a turn about X keeps "About: X" after moving to Y and sending, until the Y turn starts', async () => {
    enabled('a1')
    const rt = withBtw()
    rt.state = { ...rt.state, turns: [asked('u1', 's1')] as never, status: { state: 'in_progress', count: 0 } }
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/s2?drawer=assistant', mode: 'desktop', queryClient: seeded() })
    expect(header()).toHaveTextContent('About: Fix the flaky login test')
    expect(header()).toHaveTextContent('Next message will be about Ship the release notes')
    // Sending about Y mid-turn queues it: the server still holds X.
    await user.type(within(drawer()!).getByRole('textbox', { name: /message/i }), 'and now?{Enter}')
    expect(rt.onPrompt).toHaveBeenLastCalledWith('and now?', [], { route: '/sessions/s2', sessionId: 's2' })
    rt.state = { ...rt.state, queued: [{ text: 'and now?', images: [] }] as never }
    await act(() => app.router.navigate('/sessions/s2?drawer=assistant&r=1'))
    expect(header()).toHaveTextContent('About: Fix the flaky login test')
    // The Y turn starts: its user message carries the Y context.
    rt.state = { ...rt.state, queued: [], turns: [asked('u1', 's1'), answered('a1'), asked('u2', 's2')] as never }
    await act(() => app.router.navigate('/sessions/s2?drawer=assistant&r=2'))
    expect(header()).toHaveTextContent('About: Ship the release notes')
    expect(header()).not.toHaveTextContent(/Next message/)
  })

  it('a send taken as the answer to the assistant\'s question keeps "About: X"', async () => {
    enabled('a1')
    const rt = withBtw()
    rt.state = { ...rt.state, turns: [asked('u1', 's1'), answered('a1')] as never }
    rt.armedQuestionId = 'q1' as never
    rt.chatArmed = true
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/s2?drawer=assistant', mode: 'desktop', queryClient: seeded() })
    await user.type(within(drawer()!).getByRole('textbox', { name: /message/i }), 'yes, that one{Enter}')
    await act(() => app.router.navigate('/sessions/s2?drawer=assistant&r=1'))
    expect(header()).toHaveTextContent('About: Fix the flaky login test')
  })

  it('after a reload the header is the replayed context, not the open session', () => {
    enabled('a1')
    const rt = withBtw()
    rt.state = { ...rt.state, turns: [asked('u1', 's2'), answered('a1')] as never }
    renderApp({ path: '/sessions/s1?drawer=assistant', mode: 'desktop', queryClient: seeded() })
    expect(header()).toHaveTextContent('About: Ship the release notes')
    expect(header()).not.toHaveTextContent('Fix the flaky login test')
  })

  it('another tab\'s prompt (a user message with a context) moves the header', async () => {
    enabled('a1')
    const rt = withBtw()
    rt.state = { ...rt.state, turns: [asked('u1', 's1'), answered('a1')] as never }
    const app = renderApp({ path: '/sessions/s1?drawer=assistant', mode: 'desktop', queryClient: seeded() })
    expect(header()).toHaveTextContent('About: Fix the flaky login test')
    rt.state = { ...rt.state, turns: [asked('u1', 's1'), answered('a1'), asked('u9', 's2')] as never }
    await act(() => app.router.navigate('/sessions/s1?drawer=assistant&r=1'))
    expect(header()).toHaveTextContent('About: Ship the release notes')
  })

  it('a parked /btw whose session is no longer open is dropped, with a toast, never sent about another one', async () => {
    enabled(null)
    const rt = withBtw()
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/s1', mode: 'desktop', queryClient: seeded() })
    await user.type(dock(), '/btw why is it failing{Enter}')
    await act(() => app.router.navigate('/sessions/s2'))
    enabled('a1')
    await remount(app.router, '/sessions/s2')
    expect(rt.onPlainPrompt).not.toHaveBeenCalled()
    expect(rt.onPrompt).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith('That question was about another session — ask again here')
    await remount(app.router, '/sessions/s1')
    expect(rt.onPlainPrompt).not.toHaveBeenCalled()
  })

  it('a parked /btw with a question armed goes out as a new prompt with context, not as the answer', async () => {
    enabled('a1')
    const rt = withBtw()
    rt.armedQuestionId = 'q1' as never
    rt.chatArmed = true
    const user = userEvent.setup()
    renderApp({ path: '/sessions/s1', mode: 'desktop', queryClient: seeded() })
    await user.type(dock(), '/btw why{Enter}')
    expect(rt.onPlainPrompt).toHaveBeenCalledWith('why', [], CTX1)
    expect(rt.onPrompt).not.toHaveBeenCalled()
    expect(rt.onAnswer).not.toHaveBeenCalled()
  })

  it('a failed create names the reason instead of "API error <status>"', () => {
    // #2184: a fleet refusal reached the drawer as an `ApiRequestError`, whose
    // `message` is the literal `API error 500` — the only thing the user saw.
    // The typed body now carries why, read with the New Session dialog's copy.
    // Last in the file: it is the only case that overrides the create mock, and
    // nothing here resets mocks between tests.
    vi.mocked(useAssistantStatus).mockReturnValue(
      { data: { enabled: true, sessionId: null, model: 'sonnet' }, isLoading: false } as never,
    )
    vi.mocked(useAssistantSession).mockReturnValue({
      mutate: vi.fn(), isPending: false, isError: true,
      error: new ApiRequestError(409, {
        error: { code: 'no_host_available', message: 'no enabled, healthy host with capacity' },
      }),
    } as never)
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    const dlg = screen.getByRole('dialog', { name: /assistant/i })
    expect(within(dlg).getByText(/no enabled, healthy host with capacity/i)).toBeInTheDocument()
    expect(within(dlg).queryByText(/API error/i)).toBeNull()
  })
})
