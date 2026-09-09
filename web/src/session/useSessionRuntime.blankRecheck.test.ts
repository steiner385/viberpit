/**
 * A live session that replayed EMPTY must be re-attached exactly once.
 *
 * Why this exists: a session parked at 'queued' (no fleet capacity) is viewed while it
 * waits, so the bridge takes its read-only detached path and never pops the deferred
 * initial prompt. The recovery for that is a re-attach on the pre-spawn → live status
 * edge — but status arrives on a 5s poll, so when the first sessions payload already
 * shows the session live, that edge is never observed here and the prompt stays
 * orphaned: the card reads "Ready" over an empty conversation forever (2026-09-07).
 *
 * These pin the narrow recovery: a COMPLETED replay that produced no turns re-attaches
 * (which pops the staged prompt server-side); a replay that produced turns does not,
 * so a healthy session never pays a second full replay.
 */

import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

vi.mock('@/api/transcript', () => ({ fetchOlderTurns: vi.fn() }))

const mockAttach = vi.fn()
let capturedSessionEventHandler: ((e: unknown) => void) | undefined

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: mockAttach,
      detach: vi.fn(),
      interrupt: vi.fn(),
      prompt: vi.fn(),
      sendInterrupt: vi.fn(),
      permissionResponse: vi.fn(),
      answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(),
      cancelQueued: vi.fn(),
      interruptAndPrompt: vi.fn(),
      on: (event: string, handler: (e: unknown) => void) => {
        if (event === 'sessionEvent') capturedSessionEventHandler = handler
      },
      off: vi.fn(),
      connectionState: 'connected',
      socket: null,
    }),
  }
})

// The session record drives the effect; status is what the 5s poll would have returned.
let mockSessions: { id: string; status: string }[] = []
vi.mock('@/api/hooks', () => ({
  useSessions: () => ({ data: mockSessions }),
  useAccounts: () => ({ data: [] }),
  useSettings: () => ({ data: undefined }),
}))

import { useSessionRuntime } from './useSessionRuntime'

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return React.createElement(QueryClientProvider, { client: queryClient }, children)
}

describe('useSessionRuntime — empty-replay re-attach', () => {
  beforeEach(() => {
    mockAttach.mockReset()
    capturedSessionEventHandler = undefined
    // Already live on the FIRST payload: the queued phase is invisible to this view,
    // so the pre-spawn → live edge never fires and only this recovery can act.
    mockSessions = [{ id: 'sess1', status: 'in_progress' }]
  })

  it('re-attaches once when the replay completes with no turns', () => {
    renderHook(() => useSessionRuntime('sess1'), { wrapper })
    const afterMount = mockAttach.mock.calls.length

    act(() => { capturedSessionEventHandler?.({ type: 'replayComplete' }) })

    expect(mockAttach.mock.calls.length).toBe(afterMount + 1)
    expect(mockAttach).toHaveBeenLastCalledWith('sess1')
  })

  it('does not re-attach when the replay brought back turns', () => {
    renderHook(() => useSessionRuntime('sess1'), { wrapper })
    const afterMount = mockAttach.mock.calls.length

    act(() => {
      capturedSessionEventHandler?.({
        type: 'user', turnId: 't1', text: 'hello', images: [], ts: 1, ownerPrefix: false,
      })
      capturedSessionEventHandler?.({ type: 'replayComplete' })
    })

    expect(mockAttach.mock.calls.length).toBe(afterMount)
  })

  it('does not attach-storm an empty session that stays empty', () => {
    const { rerender } = renderHook(() => useSessionRuntime('sess1'), { wrapper })
    act(() => { capturedSessionEventHandler?.({ type: 'replayComplete' }) })
    const afterRecheck = mockAttach.mock.calls.length

    // Later polls keep reporting the same live-but-empty session.
    act(() => { rerender() })
    mockSessions = [{ id: 'sess1', status: 'waiting_external' }]
    act(() => { rerender() })

    expect(mockAttach.mock.calls.length).toBe(afterRecheck)
  })
})
