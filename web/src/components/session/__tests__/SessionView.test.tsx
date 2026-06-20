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
  input: vi.fn(),
  resize: vi.fn(),
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

  it('shows title when title event fires', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    emitSessionEvent({ type: 'title', title: 'My Session Title', source: 'claude' })

    await waitFor(() => {
      expect(screen.getByText('My Session Title')).toBeInTheDocument()
    })
  })

  it('shows error banner when error event fires', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })

    emitSessionEvent({ type: 'error', code: 'RATE_LIMITED', message: 'Too many requests' })

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/Too many requests/)
    })
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
