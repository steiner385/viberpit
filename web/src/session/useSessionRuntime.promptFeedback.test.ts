/**
 * useSessionRuntime — immediate feedback for a sent prompt.
 *
 * The hook is the only place that knows all three facts at once: whether the frame
 * left the browser, what the edge acked, and how long we have been waiting. It turns
 * those into the pending bubble's stage.
 */
import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { PROMPT_ACK_TIMEOUT_MS } from './promptStage'

vi.mock('@/api/transcript', () => ({ fetchOlderTurns: vi.fn() }))

const mockPrompt = vi.fn()
const handlers: Record<string, (e: unknown) => void> = {}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(),
      detach: vi.fn(),
      interrupt: vi.fn(),
      prompt: mockPrompt,
      sendInterrupt: vi.fn(),
      permissionResponse: vi.fn(() => true),
      answerQuestion: vi.fn(() => true),
      setPermissionMode: vi.fn(() => true),
      cancelQueued: vi.fn(),
      interruptAndPrompt: vi.fn(() => true),
      on: (event: string, handler: (e: unknown) => void) => { handlers[event] = handler },
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

const render = () => renderHook(() => useSessionRuntime('s1'), { wrapper })
const pendingTurns = (r: ReturnType<typeof render>) =>
  r.result.current.state.turns.filter((t) => t.pending !== undefined)

describe('useSessionRuntime — pending prompt feedback', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockPrompt.mockReset()
    mockPrompt.mockReturnValue('sent')
    for (const k of Object.keys(handlers)) delete handlers[k]
  })
  afterEach(() => { vi.useRealTimers() })

  it('renders the prompt as a pending bubble the moment it is sent', () => {
    const r = render()
    act(() => { r.result.current.onPrompt('build the thing', []) })
    expect(pendingTurns(r)).toHaveLength(1)
    expect(pendingTurns(r)[0].pending).toBe('sent')
  })

  it('stages a browser-buffered prompt as "buffered", not "sent"', () => {
    mockPrompt.mockReturnValue('buffered')
    const r = render()
    act(() => { r.result.current.onPrompt('offline note', []) })
    expect(pendingTurns(r)[0].pending).toBe('buffered')
  })

  it('advances the bubble to "resuming" on the edge receipt ack', () => {
    const r = render()
    act(() => { r.result.current.onPrompt('wake up', []) })
    act(() => { handlers.promptAck?.({ type: 'promptAck', sessionId: 's1', stage: 'resuming' }) })
    expect(pendingTurns(r)[0].pending).toBe('resuming')
  })

  it('advances to "delivered" on the delivery ack', () => {
    const r = render()
    act(() => { r.result.current.onPrompt('go', []) })
    act(() => { handlers.promptAck?.({ type: 'promptAck', sessionId: 's1', stage: 'received' }) })
    act(() => { handlers.promptAck?.({ type: 'promptAck', sessionId: 's1', stage: 'delivered' }) })
    expect(pendingTurns(r)[0].pending).toBe('delivered')
  })

  it('ignores an ack addressed to a different session', () => {
    const r = render()
    act(() => { r.result.current.onPrompt('go', []) })
    act(() => { handlers.promptAck?.({ type: 'promptAck', sessionId: 'other', stage: 'delivered' }) })
    expect(pendingTurns(r)[0].pending).toBe('sent')
  })

  it('returns the bubble to "buffered" when the server refuses and re-queues it', () => {
    const r = render()
    act(() => { r.result.current.onPrompt('during a drain', []) })
    act(() => {
      handlers.promptHeld?.({ type: 'promptRejected', sessionId: 's1', text: 'during a drain', reason: 'draining' })
    })
    expect(pendingTurns(r)[0].pending).toBe('buffered')
  })

  it('marks a prompt "stalled" when no ack arrives within the timeout', () => {
    const r = render()
    act(() => { r.result.current.onPrompt('into the void', []) })
    act(() => { vi.advanceTimersByTime(PROMPT_ACK_TIMEOUT_MS + 100) })
    expect(pendingTurns(r)[0].pending).toBe('stalled')
  })

  it('does not stall a prompt the edge acked in time', () => {
    const r = render()
    act(() => { r.result.current.onPrompt('acked', []) })
    act(() => { handlers.promptAck?.({ type: 'promptAck', sessionId: 's1', stage: 'received' }) })
    act(() => { vi.advanceTimersByTime(PROMPT_ACK_TIMEOUT_MS + 100) })
    expect(pendingTurns(r)[0].pending).toBe('received')
  })

  it('does not stall a prompt that is knowingly held in the browser', () => {
    // "Held until we reconnect" is already explained; a stall warning on top of it
    // would be a second, contradictory story about the same prompt.
    mockPrompt.mockReturnValue('buffered')
    const r = render()
    act(() => { r.result.current.onPrompt('offline note', []) })
    act(() => { vi.advanceTimersByTime(PROMPT_ACK_TIMEOUT_MS + 100) })
    expect(pendingTurns(r)[0].pending).toBe('buffered')
  })
})
