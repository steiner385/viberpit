/**
 * The session pane — transcript, permission/question panels, status strip.
 *
 * Was `components/session/__tests__/SessionView.test.tsx`. `SessionView` had no
 * call site after Task 17 and is deleted (Task 18 / Item 2); `MainPaneSession`
 * is what `shell/routes.tsx` renders for `/sessions/:id`, in every layout mode.
 * Every assertion below is the original one — the subject changed, the contract
 * did not.
 *
 * Two of them ("seeds the initialPrompt", "does not duplicate the seeded prompt
 * on echo") are the `PageHost` + `sessionNavState` contract: the seed is a
 * `SessionRuntimeProvider` prop now, fed from router state, so they are asserted
 * through the provider rather than through a pane prop.
 *
 * ## The harness renders the pane AND the composer
 *
 * `app.main` and `app.dock` are two regions inside ONE `SessionRuntimeProvider`
 * (`PageHost` renders the page and the dock inside it). Several cases below are
 * about the relationship between the two — a permission panel pinned ABOVE the
 * composer, a prompt that still sends while a question is armed — so the harness
 * composes them the way the shell does instead of approximating it.
 *
 * Mocks useSessionSocket the same way `App.test.tsx` does — importOriginal +
 * override just the hook. We capture the 'sessionEvent' handler via the on()
 * spy so we can feed synthetic events and assert the rendered transcript.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MainPaneSession } from './MainPaneSession'
import { SessionComposer } from '@/components/session/SessionComposer'
import {
  SessionRuntimeProvider, useSessionRuntimeContext,
} from '@/components/session/SessionRuntimeProvider'
import type { SessionEvent } from '@/session/events'

// ── Stable mock socket shared across all calls to useSessionSocket() ──────────
// Using a single object ensures spies are the same instance as what the
// component calls — letting us assert on them after render.

let capturedHandlers: Record<string, ((...args: unknown[]) => void)[]> = {}

// These control-frame mocks return true: they stand in for an OPEN socket where the
// frame is written successfully, so the optimistic dispatch (resolve card / apply mode)
// fires. On a closed socket the real methods return false and the optimistic step is
// gated off (see #505). mockClear() in beforeEach keeps this return value.
const mockPermissionResponse = vi.fn().mockReturnValue(true)
const mockAnswerQuestion = vi.fn().mockReturnValue(true)
const mockPrompt = vi.fn()
const mockSendInterrupt = vi.fn()
const mockSetPermissionMode = vi.fn().mockReturnValue(true)
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

/** The dock's composer, reading the same runtime the pane does. */
function DockComposer() {
  const rt = useSessionRuntimeContext()
  return <SessionComposer rt={rt} />
}

/** `app.main` + `app.dock` for one open session — the shell's composition. */
function renderPane(
  { sessionId = 's1', onDetach = vi.fn(), initialPrompt }:
  { sessionId?: string; onDetach?: () => void; initialPrompt?: string } = {},
) {
  return render(
    <SessionRuntimeProvider sessionId={sessionId} initialPrompt={initialPrompt}>
      <MainPaneSession
        sessionId={sessionId}
        onClosed={vi.fn()}
        onDetach={onDetach}
        onOpenSession={vi.fn()}
      />
      <DockComposer />
    </SessionRuntimeProvider>,
    { wrapper },
  )
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('MainPaneSession', () => {
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
    renderPane()
    expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument()
  })

  it('fills its parent (no viewport units) so the composer stays pinned', () => {
    // Regression guard: the single viewport-height source of truth is
    // `#root { height: 100dvh }` (index.css) — see documentScrollClamp.test.ts.
    // A pane that re-measures the viewport itself (`h-dvh`/`h-screen`) overflows
    // the moment an in-flow sibling (update banner) renders above it, making the
    // document scrollable and letting focus/scrollIntoView drag the app partly
    // off-screen (dead whitespace below the composer).
    //
    // The invariant is unchanged; the class that expresses it moved with the
    // component. `SessionView` WAS the flex child of the app column and used
    // `flex-1 min-h-0`; `MainPaneSession` is a page rendered inside `PageHost`
    // (which is itself `flex-1 min-h-0`), so it fills its parent with
    // `h-full min-h-0`. What must never appear is a viewport unit.
    const { container } = renderPane()
    const root = container.firstChild as HTMLElement
    expect(root.className).toContain('h-full')
    expect(root.className).toContain('min-h-0')
    expect(root.className).not.toContain('h-dvh')
    expect(root.className).not.toContain('h-screen')
  })

  it('shows the sessionId prefix as title when no title event received', () => {
    renderPane({ sessionId: 'abcd1234efgh' })
    // Title should be the first 8 chars of the sessionId
    expect(screen.getByText('abcd1234')).toBeInTheDocument()
  })

  it('seeds the initialPrompt as a user message immediately on open', () => {
    renderPane({ initialPrompt: 'fix the bug' })
    expect(screen.getByText('fix the bug')).toBeInTheDocument()
  })

  it('does not duplicate the seeded prompt when the SDK echoes it back', () => {
    renderPane({ initialPrompt: 'fix the bug' })
    // The real user event arrives with the server turnId; the reducer adopts the
    // optimistic seed in place rather than appending a duplicate.
    emitSessionEvent({ type: 'user', turnId: 'real-1', text: 'fix the bug' })
    expect(screen.getAllByText('fix the bug')).toHaveLength(1)
  })

  it('renders assistant text after turnStart + textDelta + turnEnd events', async () => {
    renderPane()

    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({ type: 'textDelta', turnId: 't1', text: 'Hello from assistant' })
    emitSessionEvent({ type: 'turnEnd', turnId: 't1' })

    await waitFor(() => {
      expect(screen.getByText(/Hello from assistant/)).toBeInTheDocument()
    })
  })

  it('renders a PermissionCard when permissionRequest event arrives', async () => {
    renderPane()

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

  it('pins an actionable permission panel above the composer when an open permission exists', async () => {
    renderPane()

    emitSessionEvent({ type: 'replayComplete' })
    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({
      type: 'permissionRequest',
      permId: 'perm-banner',
      turnId: 't1',
      tool: 'bash',
      toolKind: 'bash',
      input: { command: 'echo hi' },
      plan: false,
    })

    await waitFor(() => {
      // The pinned panel (always visible above the composer) carries the actionable
      // allow/deny controls — not a "scroll up to respond" banner.
      const panel = screen.getByTestId('pending-permission-panel')
      expect(panel).toBeInTheDocument()
      expect(within(panel).getByRole('button', { name: /^allow/i })).toBeInTheDocument()
    })
    expect(screen.queryByText(/scroll up to respond/i)).not.toBeInTheDocument()
  })

  it('calls permissionResponse and dispatches _resolvePermission when Yes clicked', async () => {
    const user = userEvent.setup()
    renderPane()

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

  it('live question is not rendered inline — it shows the "action needed" banner instead', async () => {
    renderPane()

    // replayComplete marks replaying=false so subsequent questions are live (live: true).
    emitSessionEvent({ type: 'replayComplete' })
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

    // Live questions are pinned in the PendingQuestionPanel above the composer
    // (Task 4), rendered interactively THERE — not inline in the transcript.
    const panel = await screen.findByTestId('pending-question-panel')
    expect(within(panel).getByText('Which color?')).toBeInTheDocument()
    expect(within(panel).getByRole('radio', { name: 'Red' })).toBeInTheDocument()
    // Exactly one copy in the DOM — pinned, not duplicated inline.
    expect(screen.getAllByText('Which color?')).toHaveLength(1)

    // Task 5: a live question alone does NOT show the "action needed" banner —
    // the pinned panel IS the affordance. Banner is reserved for open permissions.
    expect(screen.queryByText(/action needed/i)).toBeNull()
  })

  it('shows title when title event fires', async () => {
    renderPane()

    emitSessionEvent({ type: 'title', title: 'My Session Title', source: 'claude' })

    await waitFor(() => {
      expect(screen.getByText('My Session Title')).toBeInTheDocument()
    })
  })

  it('shows error banner when error sessionEvent fires', async () => {
    renderPane()

    emitSessionEvent({ type: 'error', code: 'RATE_LIMITED', message: 'Too many requests' })

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/Too many requests/)
    })
  })

  it('I3: shows error banner when direct WS error frame fires (SESSION_NOT_FOUND etc.)', async () => {
    renderPane()

    // This is a direct 'error' WS message, NOT wrapped in a sessionEvent batch.
    emitErrorEvent({ type: 'error', code: 'SESSION_NOT_FOUND', message: 'Session not found' })

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/Session not found/)
    })
  })

  it('I5: rotation divider shows reason when WsRotatedMsg includes reason', async () => {
    renderPane()

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
    renderPane()

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
    renderPane()
    const live = screen.getByLabelText('Session output')
    // Suppressed while replaying (initial state).
    expect(live).toHaveTextContent('')
    emitSessionEvent({ type: 'replayComplete' })
    await waitFor(() => expect(live).toHaveTextContent('History loaded'))
  })

  it('aria-live region mirrors the latest assistant text after replay completes', async () => {
    renderPane()
    const live = screen.getByLabelText('Session output')
    emitSessionEvent({ type: 'replayComplete' })
    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({ type: 'textDelta', turnId: 't1', text: 'the latest reply' })
    emitSessionEvent({ type: 'turnEnd', turnId: 't1' })
    await waitFor(() => expect(live).toHaveTextContent('the latest reply'))
  })

  it('renders StatusBar with Ready for idle state', () => {
    renderPane()
    expect(screen.getByRole('status')).toHaveTextContent('Ready')
  })

  // R9 successor to "single-sources live status in the meta-strip StatusBar — no
  // duplicate pill in the top bar" (Task 10).
  //
  // That case asserted `queryByLabelText(/^Status: /)` was ABSENT: the header had
  // no pill, and adding one back would have been a second, drifting copy of the
  // state the StatusBar shows. Task 10 puts a pill in `session.header` by design,
  // so the absence assertion cannot survive — but the invariant it protected can,
  // and it is the stronger half: there is still exactly ONE pill, exactly one
  // `role="status"` live region, and both read the same `session/statusTier`
  // table, so they cannot disagree. This case asserts that pair, and asserts they
  // move TOGETHER on a live status event — which the old case never did for the
  // pill, because there was no pill to move.
  it('header pill and meta-strip StatusBar are one status, from one table', async () => {
    renderPane()
    // Exactly one card-style pill — the header's. Not one per surface.
    expect(screen.getAllByLabelText(/^Status: /)).toHaveLength(1)
    // Exactly one live region — the StatusBar's. The pill is a static label, so
    // an idle→working flip is announced once, not twice.
    expect(screen.getAllByRole('status')).toHaveLength(1)

    // Idle on open → both read the `idle` row of the shared table.
    expect(screen.getByLabelText('Status: Ready')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Ready')

    // Live status flows from the runtime: a `status` event moves BOTH.
    emitSessionEvent({ type: 'status', state: 'in_progress', count: 1 })
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/Working/)
    })
    expect(screen.getByLabelText('Status: Working…')).toBeInTheDocument()
    expect(screen.queryByLabelText('Status: Ready')).not.toBeInTheDocument()
  })

  it('Back detaches the socket and calls onDetach', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane({ onDetach: onBack })
    await user.click(screen.getByRole('button', { name: /back to session list/i }))
    expect(mockDetach).toHaveBeenCalledWith('s1')
    expect(onBack).toHaveBeenCalledOnce()
  })
})

describe('MainPaneSession question escape hatches', () => {
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
    const utils = renderPane()
    // replayComplete marks replaying=false so the question is emitted as live (live: true).
    emitSessionEvent({ type: 'replayComplete' })
    emitSessionEvent({ type: 'turnStart', turnId: 't1' })
    emitSessionEvent({
      type: 'question', questionId: 'q-9', turnId: 't1', header: 'H',
      question: 'Pick one', options: [{ label: 'A' }], multiSelect: false,
    })
    return utils
  }

  // The "Chat about this" / "Write a response" escape hatches render inside the
  // PendingQuestionPanel (Task 4), which pins the live question above the composer.
  // (The hatch callbacks are unit-tested in PendingQuestionPanel.test.tsx.)

  it('live question and its escape hatches render in the pinned panel, not inline', async () => {
    renderAndAskQuestion()
    const panel = await screen.findByTestId('pending-question-panel')
    expect(within(panel).getByText('Pick one')).toBeInTheDocument()
    expect(within(panel).getByRole('button', { name: /chat about this/i })).toBeInTheDocument()
    expect(within(panel).getByRole('button', { name: /write a response/i })).toBeInTheDocument()
    // Exactly one copy — pinned, not inline in the transcript.
    expect(screen.getAllByText('Pick one')).toHaveLength(1)
    // Task 5: a live question alone does NOT show the "action needed" banner —
    // the pinned panel IS the affordance. Banner is reserved for open permissions.
    expect(screen.queryByText(/action needed/i)).toBeNull()
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
