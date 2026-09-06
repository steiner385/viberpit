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
const mockPermissionResponse = vi.fn()
const mockSetPermissionMode = vi.fn()
const mockInterruptAndPrompt = vi.fn()
let capturedSessionEventHandler: ((e: unknown) => void) | undefined
let capturedErrorHandler: ((e: unknown) => void) | undefined
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
      permissionResponse: mockPermissionResponse,
      answerQuestion: mockAnswerQuestion,
      setPermissionMode: mockSetPermissionMode,
      cancelQueued: vi.fn(),
      interruptAndPrompt: mockInterruptAndPrompt,
      on: (event: string, handler: (e: unknown) => void) => {
        if (event === 'sessionEvent') capturedSessionEventHandler = handler
        if (event === 'error') capturedErrorHandler = handler
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
    mockPermissionResponse.mockReset()
    mockPermissionResponse.mockReturnValue(true)
    mockSetPermissionMode.mockReset()
    mockSetPermissionMode.mockReturnValue(true)
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

    // Answer it — WS send fires + armed id is cleared + the block is resolved IN PLACE.
    act(() => { result.current.onAnswer('q2', ['Yes']) })
    expect(mockAnswerQuestion).toHaveBeenCalledWith('sess2', 'q2', ['Yes'], 'option')
    expect(result.current.armedQuestionId).toBeNull()
    // The question block stays inline (anchored where it was asked), now marked answered
    // and no longer live — NOT removed, and NOT echoed as a tail user turn.
    const q = result.current.state.turns.flatMap((t) => t.blocks).find((b) => b.kind === 'question' && b.questionId === 'q2')
    expect(q && q.kind === 'question' && q.answered).toEqual(['Yes'])
    expect(q && q.kind === 'question' && q.live).toBe(false)
    expect(result.current.state.turns.some((t) => t.role === 'user')).toBe(false)
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

  it('does NOT auto-resend a rejected answer on reconnect — no stale replay (#505)', async () => {
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
    expect(mockAnswerQuestion).toHaveBeenCalledTimes(1)   // attempted once, rejected
    expect(result.current.state.error?.code).toBe('answer_not_sent')
    expect(qExists('q4')).toBe(true)                       // kept interactive for retry

    // Reconnect: socket becomes open. A stale buffered answer must NOT be auto-replayed
    // (the server would reconcile it onto whatever question is pending NOW — wrong-answer
    // risk). So no further answerQuestion frame goes out on reopen.
    mockAnswerQuestion.mockReturnValue(true)
    act(() => { mockConnectionState = 'connected'; rerender() })

    expect(mockAnswerQuestion).toHaveBeenCalledTimes(1)    // still just the one attempt
    // The question stays open + the not-sent banner stays up until the user retries.
    expect(qExists('q4')).toBe(true)
    const q4 = result.current.state.turns.flatMap((t) => t.blocks).find((b) => b.kind === 'question' && b.questionId === 'q4')
    expect(q4 && q4.kind === 'question' && q4.live).toBe(true)
    expect(result.current.state.error?.code).toBe('answer_not_sent')

    // A manual retry now succeeds, resolves the card, and clears the banner.
    act(() => { result.current.onAnswer('q4', ['Yes']) })
    expect(mockAnswerQuestion).toHaveBeenCalledTimes(2)
    const q4b = result.current.state.turns.flatMap((t) => t.blocks).find((b) => b.kind === 'question' && b.questionId === 'q4')
    expect(q4b && q4b.kind === 'question' && q4b.live).toBe(false)
    expect(q4b && q4b.kind === 'question' && q4b.answered).toEqual(['Yes'])
    expect(result.current.state.error).toBeNull()          // banner cleared on confirmed send
  })
})

describe('useSessionRuntime.onDecision (permission) — reject on closed socket (#505)', () => {
  beforeEach(() => {
    mockPermissionResponse.mockReset()
    mockAnswerQuestion.mockReset()
    mockSetPermissionMode.mockReset()
    mockConnectionState = 'connected'
    capturedSessionEventHandler = undefined
  })

  function seedPermission(result: { current: { state: { turns: unknown[] } } }) {
    act(() => {
      capturedSessionEventHandler?.({ type: 'replayComplete' })
      capturedSessionEventHandler?.({
        type: 'permissionRequest',
        permId: 'perm-1',
        turnId: 'turn-1',
        tool: 'Bash',
        toolKind: 'bash',
        input: { command: 'ls' },
        plan: false,
      })
    })
    const hasCard = (result.current.state.turns as { blocks: { kind: string; permId?: string }[] }[])
      .some((t) => t.blocks.some((b) => b.kind === 'permission' && b.permId === 'perm-1'))
    expect(hasCard).toBe(true)
  }
  const permExists = (result: { current: { state: { turns: { blocks: { kind: string; permId?: string }[] }[] } } }) =>
    result.current.state.turns.some((t) => t.blocks.some((b) => b.kind === 'permission' && b.permId === 'perm-1'))

  it('resolves the card optimistically when the send succeeds (open socket)', () => {
    mockPermissionResponse.mockReturnValue(true)
    const { result } = renderHook(() => useSessionRuntime('psess1'), { wrapper })
    seedPermission(result)

    act(() => { result.current.onDecision('perm-1', 'allow') })

    expect(mockPermissionResponse).toHaveBeenCalledWith('psess1', 'perm-1', 'allow')
    expect(permExists(result)).toBe(false) // resolved
  })

  it('does NOT resolve the card when the send is rejected (closed socket)', () => {
    mockPermissionResponse.mockReturnValue(false)
    const { result } = renderHook(() => useSessionRuntime('psess2'), { wrapper })
    seedPermission(result)

    act(() => { result.current.onDecision('perm-1', 'allow') })

    expect(mockPermissionResponse).toHaveBeenCalledWith('psess2', 'perm-1', 'allow')
    expect(permExists(result)).toBe(true) // card kept interactive — NOT falsely resolved
  })
})

describe('useSessionRuntime.onSetMode — reject on closed socket (#505)', () => {
  beforeEach(() => {
    mockSetPermissionMode.mockReset()
    mockPermissionResponse.mockReset()
    mockAnswerQuestion.mockReset()
    mockConnectionState = 'connected'
    capturedSessionEventHandler = undefined
  })

  it('applies the mode locally when the send succeeds (open socket)', () => {
    mockSetPermissionMode.mockReturnValue(true)
    const { result } = renderHook(() => useSessionRuntime('msess1'), { wrapper })

    act(() => { result.current.onSetMode('plan') })

    expect(mockSetPermissionMode).toHaveBeenCalledWith('msess1', 'plan')
    expect(result.current.mode).toBe('plan')
  })

  it('does NOT apply the mode when the send is rejected (closed socket)', () => {
    mockSetPermissionMode.mockReturnValue(false)
    const { result } = renderHook(() => useSessionRuntime('msess2'), { wrapper })

    expect(result.current.mode).toBe('interactive')
    act(() => { result.current.onSetMode('plan') })

    expect(mockSetPermissionMode).toHaveBeenCalledWith('msess2', 'plan')
    expect(result.current.mode).toBe('interactive') // unchanged — no false optimistic mode
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

describe('useSessionRuntime — error frames are session-scoped', () => {
  beforeEach(() => {
    mockConnectionState = 'connected'
    capturedErrorHandler = undefined
  })

  const errorFrame = (extra: Record<string, unknown>) => ({
    type: 'error', code: 'boom', message: 'something failed', ...extra,
  })

  it('ignores an error frame addressed to a different session', () => {
    const { result } = renderHook(() => useSessionRuntime('esess1'), { wrapper })

    // An async failure in ANOTHER session (its frame carries that session's id)
    // must not paint this view's error banner.
    act(() => { capturedErrorHandler?.(errorFrame({ sessionId: 'other-session' })) })

    expect(result.current.state.error).toBeNull()
  })

  it('applies an error frame tagged for this session', () => {
    const { result } = renderHook(() => useSessionRuntime('esess2'), { wrapper })

    act(() => { capturedErrorHandler?.(errorFrame({ sessionId: 'esess2' })) })

    expect(result.current.state.error?.code).toBe('boom')
  })

  it('applies an untagged error frame (connection-level / legacy)', () => {
    const { result } = renderHook(() => useSessionRuntime('esess3'), { wrapper })

    act(() => { capturedErrorHandler?.(errorFrame({})) })

    expect(result.current.state.error?.code).toBe('boom')
  })
})

describe('useSessionRuntime.onInterruptAndPrompt', () => {
  beforeEach(() => {
    mockInterruptAndPrompt.mockReset()
    mockInterruptAndPrompt.mockReturnValue(true)
  })

  it('sends the prompt as an interrupt-and-post control frame and reports the send result', () => {
    const { result } = renderHook(() => useSessionRuntime('sess-ias'), { wrapper })
    let sent: boolean | undefined
    act(() => { sent = result.current.onInterruptAndPrompt('do this instead', ['att1']) })
    expect(mockInterruptAndPrompt).toHaveBeenCalledWith('sess-ias', 'do this instead', ['att1'])
    expect(sent).toBe(true)
  })

  it('returns false (draft kept by the composer) when the socket rejects the frame', () => {
    mockInterruptAndPrompt.mockReturnValue(false)
    const { result } = renderHook(() => useSessionRuntime('sess-ias2'), { wrapper })
    let sent: boolean | undefined
    act(() => { sent = result.current.onInterruptAndPrompt('now', []) })
    expect(sent).toBe(false)
  })
})
