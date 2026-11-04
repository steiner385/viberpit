/**
 * An undone rewind (D-9) restored the cut turns on disk; the runtime re-attaches once so
 * the replay shows them — and only once per rewind, so a re-delivered event cannot loop.
 */
import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const attach = vi.fn()
let onEvent: ((e: unknown) => void) | undefined

vi.mock('@/ws/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/ws/client')>()),
  useSessionSocket: () => ({
    attach, detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(), sendInterrupt: vi.fn(),
    permissionResponse: vi.fn(), answerQuestion: vi.fn(), setPermissionMode: vi.fn(), cancelQueued: vi.fn(),
    interruptAndPrompt: vi.fn(), command: vi.fn(), activity: vi.fn(),
    on: (event: string, handler: (e: unknown) => void) => { if (event === 'sessionEvent') onEvent = handler },
    off: vi.fn(), connectionState: 'connected', socket: null,
  }),
}))

import { useSessionRuntime } from './useSessionRuntime'

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client: new QueryClient() }, children)
}

describe('useSessionRuntime — rewindUndone', () => {
  it('re-attaches once per undone rewind, and clears the divider', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    expect(attach).toHaveBeenCalledTimes(1) // the mount attach
    act(() => onEvent?.({ type: 'replayComplete', sessionId: 's1' }))
    act(() => onEvent?.({ type: 'user', sessionId: 's1', turnId: 'u1', text: 'hi' }))
    act(() => onEvent?.({ type: 'rewound', sessionId: 's1', rewindId: 'r1', toTurnId: 'u1', promptUuid: 'P1', filesRestored: [] }))
    expect(result.current.state.rewind?.rewindId).toBe('r1')
    expect(attach).toHaveBeenCalledTimes(1) // a cut it could make itself needs no replay

    act(() => onEvent?.({ type: 'rewindUndone', sessionId: 's1', rewindId: 'r1' }))
    expect(result.current.state.rewind).toBeNull()
    expect(attach).toHaveBeenCalledTimes(2)
    expect(attach).toHaveBeenLastCalledWith('s1')

    act(() => onEvent?.({ type: 'rewindUndone', sessionId: 's1', rewindId: 'r1' }))
    expect(attach).toHaveBeenCalledTimes(2)
  })

  it('replays once, and still sets the divider marker, for a rewind whose target this view does not hold', () => {
    attach.mockClear()
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => onEvent?.({ type: 'replayComplete', sessionId: 's1' }))
    const ev = { type: 'rewound', sessionId: 's1', rewindId: 'r2', toTurnId: 'x', promptUuid: 'y', filesRestored: [] }
    act(() => onEvent?.(ev))
    expect(result.current.state.rewind).toEqual({ rewindId: 'r2', filesRestored: [] })
    expect(attach).toHaveBeenCalledTimes(2)
    act(() => onEvent?.(ev))
    expect(attach).toHaveBeenCalledTimes(2)
  })

  it('dismissRewind drops the divider (the server said the undo expired)', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => onEvent?.({ type: 'replayComplete', sessionId: 's1' }))
    act(() => onEvent?.({ type: 'user', sessionId: 's1', turnId: 'u1', text: 'hi' }))
    act(() => onEvent?.({ type: 'rewound', sessionId: 's1', rewindId: 'r3', toTurnId: 'u1', promptUuid: 'P', filesRestored: [] }))
    act(() => result.current.dismissRewind('r3'))
    expect(result.current.state.rewind).toBeNull()
  })

  it("ignores another session's undo", () => {
    attach.mockClear()
    renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => onEvent?.({ type: 'rewindUndone', sessionId: 'other', rewindId: 'r9' }))
    expect(attach).toHaveBeenCalledTimes(1)
  })
})
