/**
 * SessionView tests.
 *
 * Mocks useSessionSocket the same way App.test.tsx does — importOriginal +
 * override just the hook. We capture the 'sessionEvent' handler via the on()
 * spy so we can feed synthetic events and assert the rendered transcript.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionView } from '../SessionView'
import type { SessionEvent } from '@/session/events'

// ── Stable mock socket shared across all calls to useSessionSocket() ──────────
// Using a single object ensures spies are the same instance as what the
// component calls — letting us assert on them after render.

let capturedHandlers: Record<string, ((...args: unknown[]) => void)[]> = {}

const mockPermissionResponse = vi.fn()
const mockAnswerQuestion = vi.fn()
const mockPrompt = vi.fn()
const mockSendInterrupt = vi.fn()
const mockSetPermissionMode = vi.fn()
const mockAttach = vi.fn()
const mockDetach = vi.fn()
const mockOff = vi.fn()

const mockOn = vi.fn().mockImplementation((event: string, cb: (...args: unknown[]) => void) => {
  if (!capturedHandlers[event]) capturedHandlers[event] = []
  capturedHandlers[event].push(cb)
})

const stableMockSocket = {
  attach: mockAttach,
  detach: mockDetach,
  interrupt: vi.fn(),
  prompt: mockPrompt,
  sendInterrupt: mockSendInterrupt,
  permissionResponse: mockPermissionResponse,
  answerQuestion: mockAnswerQuestion,
  setPermissionMode: mockSetPermissionMode,
  on: mockOn,
  off: mockOff,
  connectionState: 'connected' as const,
  socket: null,
}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => stableMockSocket,
  }
})

// ── Helper to feed session events ─────────────────────────────────────────────

function emitSessionEvent(event: SessionEvent) {
  for (const handler of capturedHandlers['sessionEvent'] ?? []) {
    act(() => { handler(event) })
  }
}

function emitErrorEvent(msg: { type: 'error'; sessionId?: string; code: string; message: string; resetsAt?: string | null }) {
  for (const handler of capturedHandlers['error'] ?? []) {
    act(() => { handler(msg) })
  }
}

function emitRotatedEvent(msg: { type: 'rotated'; sessionId: string; fromAccount: string; toAccount: string; reason?: string; turnRestarted?: boolean }) {
  for (const handler of capturedHandlers['rotated'] ?? []) {
    act(() => { handler(msg) })
  }
}

// ── Wrapper ───────────────────────────────────────────────────────────────────

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('SessionView', () => {
  beforeEach(() => {
    capturedHandlers = {}
    // Reset individual spies manually because vi.clearAllMocks() would also
    // clear mockOn's implementation, breaking handler capture.
    mockPermissionResponse.mockClear()
    mockAnswerQuestion.mockClear()
    mockPrompt.mockClear()
    mockSendInterrupt.mockClear()
    mockSetPermissionMode.mockClear()
    mockAttach.mockClear()
    mockDetach.mockClear()
    mockOff.mockClear()
    mockOn.mockClear()
    // Restore mockOn's implementation after clearing
    mockOn.mockImplementation((event: string, cb: (...args: unknown[]) => void) => {
      if (!capturedHandlers[event]) capturedHandlers[event] = []
      capturedHandlers[event].push(cb)
    })
  })

  it('renders the Back button with the correct aria-label', () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument()
  })

  it('uses dynamic viewport height (h-dvh) so the top bar and composer stay pinned on mobile', () => {
    // Regression guard: `h-screen` (=100vh) is the *large* viewport on mobile, so
    // while the browser toolbar is shown the composer is pushed behind it and the
    // whole document scrolls — dragging the "static" top bar off-screen. `h-dvh`
    // tracks the currently-visible viewport, keeping top/bottom pinned and only the
    // transcript scrollable.
    const { container } = render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    const root = container.firstChild as HTMLElement
    expect(root.className).toContain('h-dvh')
    expect(root.className).not.toContain('h-screen')
  })

  it('shows the sessionId prefix as title when no title event received', () => {
    render(<SessionView sessionId="abcd1234efgh" onBack={vi.fn()} />, { wrapper })
    // Title should be the first 8 chars of the sessionId
    expect(screen.getByText('abcd1234')).toBeInTheDocument()
  })

  it('renders assistant text after turnStart + textDelta + turnEnd events', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({ type: 'textDelta', turnId: 't1', text: 'Hello from assistant' })
    emitSessionEvent({ type: 'turnEnd', turnId: 't1' })

    await waitFor(() => {
      expect(screen.getByText(/Hello from assistant/)).toBeInTheDocument()
    })
  })

  it('renders a PermissionCard when permissionRequest event arrives', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({
      type: 'permissionRequest',
      permId: 'perm-1',
      turnId: 't1',
      tool: 'bash',
      toolKind: 'bash',
      input: { command: 'ls -la' },
      plan: false,
    })

    await waitFor(() => {
      // PermissionCard renders as role="group" with the permission label
      expect(screen.getByRole('group', { name: /permission request/i })).toBeInTheDocument()
    })
  })

  it('calls permissionResponse and dispatches _resolvePermission when Yes clicked', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({
      type: 'permissionRequest',
      permId: 'perm-1',
      turnId: 't1',
      tool: 'bash',
      toolKind: 'bash',
      input: { command: 'ls -la' },
      plan: false,
    })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^allow/i })).toBeInTheDocument()
    })

    await user.click(screen.getByRole('button', { name: /^allow/i }))

    expect(mockPermissionResponse).toHaveBeenCalledWith('s1', 'perm-1', 'allow')

    // Card should be removed after decision
    await waitFor(() => {
      expect(screen.queryByRole('group', { name: /permission request/i })).not.toBeInTheDocument()
    })
  })

  it('echoes the chosen answer into the transcript when a question is answered', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({
      type: 'question',
      questionId: 'q-1',
      turnId: 't1',
      header: 'Pick one',
      question: 'Which color?',
      options: [{ label: 'Red' }, { label: 'Blue' }],
      multiSelect: false,
    })

    await waitFor(() => {
      expect(screen.getByRole('radio', { name: 'Blue' })).toBeInTheDocument()
    })

    await user.click(screen.getByRole('radio', { name: 'Blue' }))

    // Answer is sent to the server (default 'option' mode for a picked option).
    expect(mockAnswerQuestion).toHaveBeenCalledWith('s1', 'q-1', ['Blue'], 'option')

    // The question card is removed…
    await waitFor(() => {
      expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
    })

    // …and the chosen answer is echoed into the transcript as immediate feedback,
    // so the user sees their response register (the card label is gone by now, so
    // a remaining "Blue" can only be the echoed user bubble).
    await waitFor(() => {
      expect(screen.getByText('Blue')).toBeInTheDocument()
    })
  })

  it('shows title when title event fires', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    emitSessionEvent({ type: 'title', title: 'My Session Title', source: 'claude' })

    await waitFor(() => {
      expect(screen.getByText('My Session Title')).toBeInTheDocument()
    })
  })

  it('shows error banner when error sessionEvent fires', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    emitSessionEvent({ type: 'error', code: 'RATE_LIMITED', message: 'Too many requests' })

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/Too many requests/)
    })
  })

  it('I3: shows error banner when direct WS error frame fires (SESSION_NOT_FOUND etc.)', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    // This is a direct 'error' WS message, NOT wrapped in a sessionEvent batch.
    emitErrorEvent({ type: 'error', code: 'SESSION_NOT_FOUND', message: 'Session not found' })

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/Session not found/)
    })
  })

  it('I5: rotation divider shows reason when WsRotatedMsg includes reason', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    emitRotatedEvent({
      type: 'rotated',
      sessionId: 's1',
      fromAccount: 'alice',
      toAccount: 'bob',
      reason: 'rate limited',
    })

    await waitFor(() => {
      expect(screen.getByText(/switched alice → bob — rate limited/)).toBeInTheDocument()
    })
  })

  it('interleaves the rotation divider inline between the turns it occurred between', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    // Turn 1, then a rotation, then turn 2.
    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({ type: 'textDelta', turnId: 't1', text: 'before rotation' })
    emitSessionEvent({ type: 'turnEnd', turnId: 't1' })
    emitRotatedEvent({ type: 'rotated', sessionId: 's1', fromAccount: 'alice', toAccount: 'bob' })
    emitSessionEvent({ type: 'turnStart', turnId: 't2' })
    emitSessionEvent({ type: 'textDelta', turnId: 't2', text: 'after rotation' })
    emitSessionEvent({ type: 'turnEnd', turnId: 't2' })

    const divider = await screen.findByRole('separator', { name: /account switched from alice to bob/i })
    const before = screen.getByText(/before rotation/)
    const after = screen.getByText(/after rotation/)

    // DOM order: before-turn → divider → after-turn.
    // Node.DOCUMENT_POSITION_FOLLOWING (4) means the argument follows the node.
    expect(before.compareDocumentPosition(divider) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(divider.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('aria-live region announces "History loaded" once replay completes', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    const live = screen.getByLabelText('Session output')
    // Suppressed while replaying (initial state).
    expect(live).toHaveTextContent('')
    emitSessionEvent({ type: 'replayComplete' })
    await waitFor(() => expect(live).toHaveTextContent('History loaded'))
  })

  it('aria-live region mirrors the latest assistant text after replay completes', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    const live = screen.getByLabelText('Session output')
    emitSessionEvent({ type: 'replayComplete' })
    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({ type: 'textDelta', turnId: 't1', text: 'the latest reply' })
    emitSessionEvent({ type: 'turnEnd', turnId: 't1' })
    await waitFor(() => expect(live).toHaveTextContent('the latest reply'))
  })

  it('renders StatusBar with Ready for idle state', () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    expect(screen.getByRole('status')).toHaveTextContent('Ready')
  })

  it('calls onBack when Back button is clicked', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />, { wrapper })
    await user.click(screen.getByRole('button', { name: /back to session list/i }))
    expect(onBack).toHaveBeenCalledOnce()
  })
})

describe('SessionView question escape hatches', () => {
  beforeEach(() => {
    capturedHandlers = {}
    mockPermissionResponse.mockClear()
    mockAnswerQuestion.mockClear()
    mockPrompt.mockClear()
    mockSendInterrupt.mockClear()
    mockSetPermissionMode.mockClear()
    mockAttach.mockClear()
    mockDetach.mockClear()
    mockOff.mockClear()
    mockOn.mockClear()
    mockOn.mockImplementation((event: string, cb: (...args: unknown[]) => void) => {
      if (!capturedHandlers[event]) capturedHandlers[event] = []
      capturedHandlers[event].push(cb)
    })
  })

  function renderAndAskQuestion() {
    const utils = render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({
      type: 'question', questionId: 'q-9', turnId: 't1', header: 'H',
      question: 'Pick one', options: [{ label: 'A' }], multiSelect: false,
    })
    return utils
  }

  it('chat: arming routes the next composer message to answerQuestion(mode=chat)', async () => {
    const user = userEvent.setup()
    renderAndAskQuestion()
    await user.click(screen.getByRole('button', { name: /chat about this/i }))
    // Card collapsed to banner.
    expect(screen.getByText(/replying in chat/i)).toBeInTheDocument()
    // Type in the composer and send.
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'lets talk instead')
    // While armed for chat the composer offers a "Reply" affordance (not Queue/Send).
    await user.click(screen.getByRole('button', { name: /send reply/i }))
    expect(mockAnswerQuestion).toHaveBeenCalledWith('s1', 'q-9', ['lets talk instead'], 'chat')
    expect(mockPrompt).not.toHaveBeenCalled()
  })

  it('free-form: inline response calls answerQuestion(mode=freeform)', async () => {
    const user = userEvent.setup()
    renderAndAskQuestion()
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    await user.type(screen.getByRole('textbox', { name: /write a response/i }), 'my words')
    await user.click(screen.getByRole('button', { name: /send response/i }))
    expect(mockAnswerQuestion).toHaveBeenCalledWith('s1', 'q-9', ['my words'], 'freeform')
  })

  it('normal prompt is unaffected when no question is armed', async () => {
    const user = userEvent.setup()
    renderAndAskQuestion()
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'normal message')
    await user.click(screen.getByRole('button', { name: /send message/i }))
    expect(mockPrompt).toHaveBeenCalledWith('s1', 'normal message', [])
    expect(mockAnswerQuestion).not.toHaveBeenCalled()
  })
})
