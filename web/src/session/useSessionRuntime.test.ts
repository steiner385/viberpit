/**
 * useSessionRuntime.onAnswer guard tests.
 *
 * Verifies that `onAnswer`:
 *   1. sends via `answerQuestion` (the WS boundary) immediately and synchronously
 *   2. dispatches `_resolveQuestion` so the question block is removed from state
 *
 * Also verifies `onLoadOlder`:
 *   - is a no-op when `earliestTurn === 0` (fresh runtime / already at session start)
 */

import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

vi.mock('@/api/transcript', () => ({
  fetchOlderTurns: vi.fn(),
}))

// Capture answerQuestion and the sessionEvent handler at module scope
// so the hoisted vi.mock factory can close over them.
const mockAnswerQuestion = vi.fn()
let capturedSessionEventHandler: ((e: unknown) => void) | undefined
// Controllable so tests can simulate a reconnect (reconnecting → connected).
let mockConnectionState: 'connecting' | 'connected' | 'disconnected' | 'reconnecting' = 'connected'

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(),
      detach: vi.fn(),
      interrupt: vi.fn(),
      prompt: vi.fn(),
      sendInterrupt: vi.fn(),
      permissionResponse: vi.fn(),
      answerQuestion: mockAnswerQuestion,
      setPermissionMode: vi.fn(),
      cancelQueued: vi.fn(),
      on: (event: string, handler: (e: unknown) => void) => {
        if (event === 'sessionEvent') capturedSessionEventHandler = handler
      },
      off: vi.fn(),
      connectionState: mockConnectionState,
      socket: null,
    }),
  }
})

import { useSessionRuntime } from './useSessionRuntime'

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return React.createElement(QueryClientProvider, { client: queryClient }, children)
}

describe('useSessionRuntime.onAnswer', () => {
  beforeEach(() => {
    mockAnswerQuestion.mockReset()
    mockAnswerQuestion.mockReturnValue(true) // socket open: send succeeds by default
    mockConnectionState = 'connected'
    capturedSessionEventHandler = undefined
  })

  it('sends the answer immediately over WS and resolves the question block', () => {
    const { result } = renderHook(() => useSessionRuntime('sess1'), { wrapper })

    // The runtime provides onAnswer — call it directly.
    act(() => {
      result.current.onAnswer('q1', ['Yes'])
    })

    // 1. WS boundary: answerQuestion must have been called with the right args.
    expect(mockAnswerQuestion).toHaveBeenCalledWith('sess1', 'q1', ['Yes'], 'option')

    // 2. freeform mode is forwarded correctly.
    mockAnswerQuestion.mockClear()
    act(() => {
      result.current.onAnswer('q1', ['No'], 'freeform')
    })
    expect(mockAnswerQuestion).toHaveBeenCalledWith('sess1', 'q1', ['No'], 'freeform')
  })

  it('clears armedQuestionId after onAnswer when the question is in state', () => {
    const { result } = renderHook(() => useSessionRuntime('sess2'), { wrapper })

    // Seed a question block into state via the captured sessionEvent handler.
    // The handler is registered by the useEffect in useSessionRuntime.
    act(() => {
      capturedSessionEventHandler?.({ type: 'replayComplete' })
      capturedSessionEventHandler?.({
        type: 'question',
        questionId: 'q2',
        turnId: 'turn-1',
        header: 'Q',
        question: 'Q?',
        options: [{ label: 'Yes' }],
        multiSelect: false,
      })
    })

    // The question block is now live (post-replayComplete). Arm it for chat.
    act(() => { result.current.onChat('q2') })
    expect(result.current.armedQuestionId).toBe('q2')

    // Answer it — WS send fires + armed id is cleared + the block is removed from state.
    act(() => { result.current.onAnswer('q2', ['Yes']) })
    expect(mockAnswerQuestion).toHaveBeenCalledWith('sess2', 'q2', ['Yes'], 'option')
    expect(result.current.armedQuestionId).toBeNull()
    // Assert the question block is no longer in state (cleared by _resolveQuestion).
    expect(result.current.state.turns.flatMap((t) => t.blocks).some((b) => b.kind === 'question' && b.questionId === 'q2')).toBe(false)
  })

  it('keeps the question and surfaces a notice when the WS send is dropped', () => {
    mockAnswerQuestion.mockReturnValue(false) // socket not open → frame dropped
    const { result } = renderHook(() => useSessionRuntime('sess3'), { wrapper })

    act(() => {
      capturedSessionEventHandler?.({ type: 'replayComplete' })
      capturedSessionEventHandler?.({
        type: 'question',
        questionId: 'q3',
        turnId: 'turn-1',
        header: 'Q',
        question: 'Q?',
        options: [{ label: 'Yes' }],
        multiSelect: false,
      })
    })

    act(() => { result.current.onAnswer('q3', ['Yes']) })

    // The answer never left the browser — the question must stay in state so the
    // user can retry, and a notice must be surfaced.
    expect(result.current.state.turns.flatMap((t) => t.blocks).some((b) => b.kind === 'question' && b.questionId === 'q3')).toBe(true)
    expect(result.current.state.error?.code).toBe('answer_not_sent')
  })

  it('auto-resends a buffered answer and clears the notice when the socket reconnects', async () => {
    const qExists = (id: string) =>
      result.current.state.turns.flatMap((t) => t.blocks).some((b) => b.kind === 'question' && b.questionId === id)

    mockConnectionState = 'reconnecting'
    mockAnswerQuestion.mockReturnValue(false) // dropped while reconnecting
    const { result, rerender } = renderHook(() => useSessionRuntime('sess4'), { wrapper })

    act(() => {
      capturedSessionEventHandler?.({ type: 'replayComplete' })
      capturedSessionEventHandler?.({
        type: 'question',
        questionId: 'q4',
        turnId: 'turn-1',
        header: 'Q',
        question: 'Q?',
        options: [{ label: 'Yes' }],
        multiSelect: false,
      })
    })

    act(() => { result.current.onAnswer('q4', ['Yes']) })
    expect(mockAnswerQuestion).toHaveBeenCalledTimes(1)   // attempted once, dropped
    expect(result.current.state.error?.code).toBe('answer_not_sent')
    expect(qExists('q4')).toBe(true)                       // retained for retry

    // Reconnect: socket open, send now succeeds.
    mockAnswerQuestion.mockReturnValue(true)
    act(() => { mockConnectionState = 'connected'; rerender() })

    expect(mockAnswerQuestion).toHaveBeenCalledTimes(2)    // auto-resent on reconnect
    expect(mockAnswerQuestion).toHaveBeenLastCalledWith('sess4', 'q4', ['Yes'], 'option')
    expect(qExists('q4')).toBe(false)                      // resolved after resend
    expect(result.current.state.error).toBeNull()          // notice cleared
  })
})

describe('useSessionRuntime.onLoadOlder', () => {
  beforeEach(() => {
    mockAnswerQuestion.mockReset()
    mockConnectionState = 'connected'
    capturedSessionEventHandler = undefined
  })

  it('is a no-op when earliestTurn === 0 (fresh runtime — already at session start)', async () => {
    const { fetchOlderTurns } = await import('@/api/transcript')
    vi.mocked(fetchOlderTurns).mockResolvedValue({ events: [], earliestTurn: 0, totalTurns: 0 })

    const { result } = renderHook(() => useSessionRuntime('sid-older'), { wrapper })

    // On a fresh runtime, state.earliestTurn === 0 — onLoadOlder must be a no-op.
    await act(async () => { result.current.onLoadOlder() })

    expect(fetchOlderTurns).not.toHaveBeenCalled()
  })

  it('calls fetchOlderTurns with the session id and earliestTurn when extent is known', async () => {
    const { fetchOlderTurns } = await import('@/api/transcript')
    vi.mocked(fetchOlderTurns).mockResolvedValue({
      events: [],
      earliestTurn: 0,
      totalTurns: 21,
    })

    const { result } = renderHook(() => useSessionRuntime('sid-older2'), { wrapper })

    // Drive earliestTurn into state via a replayComplete event with extent metadata.
    act(() => {
      capturedSessionEventHandler?.({ type: 'replayComplete', earliestTurn: 10, totalTurns: 21 })
    })

    await act(async () => { result.current.onLoadOlder() })

    expect(fetchOlderTurns).toHaveBeenCalledWith('sid-older2', 10)
  })
})
