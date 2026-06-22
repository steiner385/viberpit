/**
 * useSessionRuntime.onAnswer guard tests.
 *
 * Verifies that `onAnswer`:
 *   1. sends via `answerQuestion` (the WS boundary) immediately and synchronously
 *   2. dispatches `_resolveQuestion` so the question block is removed from state
 */

import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

// Capture answerQuestion and the sessionEvent handler at module scope
// so the hoisted vi.mock factory can close over them.
const mockAnswerQuestion = vi.fn()
let capturedSessionEventHandler: ((e: unknown) => void) | undefined

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
      connectionState: 'connected' as const,
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
    mockAnswerQuestion.mockClear()
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
})
