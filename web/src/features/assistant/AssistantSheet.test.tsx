import type { ReactNode } from 'react'
import { fireEvent, screen, within } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'

vi.mock('@/api/assistant', () => ({
  useAssistantStatus: vi.fn(),
  useAssistantSession: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false, isError: false, error: null })),
  useResetAssistant: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useSetAssistantModel: vi.fn(() => ({
    mutateAsync: vi.fn(), isPending: false, isError: false, error: null,
  })),
  useAssistantHistory: vi.fn(() => ({ data: [], isLoading: false })),
  useResumeAssistantSession: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false, isError: false })),
}))

// The real SessionRuntimeProvider owns a live WebSocket attach/detach cycle
// (useSessionRuntime) — well outside what this test needs to prove (states +
// wiring). Mocked as a dumb wrapper, alongside the context hook its descendants
// read, so `AssistantSheetBody` gets a fully-formed (fake) SessionRuntime without
// a real socket.
vi.mock('@/features/sessions/SessionRuntimeProvider', () => ({
  SessionRuntimeProvider: ({ children }: { children: ReactNode }) => <div data-testid="runtime">{children}</div>,
  useSessionRuntimeContext: vi.fn(),
}))

import { useAssistantStatus } from '@/api/assistant'
import { useSessionRuntimeContext } from '@/features/sessions/SessionRuntimeProvider'
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
    rt.hasOpenPermission = true
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
})
