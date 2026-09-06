import type { ReactNode } from 'react'
import { screen, within } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'

vi.mock('@/api/assistant', () => ({
  useAssistantStatus: vi.fn(),
  useAssistantSession: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false, isError: false, error: null })),
  useResetAssistant: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useSetAssistantModel: vi.fn(() => ({
    mutateAsync: vi.fn(), isPending: false, isError: false, error: null,
  })),
}))

// The real SessionRuntimeProvider owns a live WebSocket attach/detach cycle
// (useSessionRuntime) — well outside what this test needs to prove (states +
// wiring). Mocked as a dumb wrapper, alongside the context hook its descendants
// read, so `AssistantSheetBody` gets a fully-formed (fake) SessionRuntime without
// a real socket.
vi.mock('@/components/session/SessionRuntimeProvider', () => ({
  SessionRuntimeProvider: ({ children }: { children: ReactNode }) => <div data-testid="runtime">{children}</div>,
  useSessionRuntimeContext: vi.fn(),
}))

import { useAssistantStatus } from '@/api/assistant'
import { useSessionRuntimeContext } from '@/components/session/SessionRuntimeProvider'
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
})
