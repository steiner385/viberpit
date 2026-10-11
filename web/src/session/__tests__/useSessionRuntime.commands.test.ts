// web/src/session/__tests__/useSessionRuntime.commands.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

// Mock the socket hook so we can drive inbound commands/commandResult.
const listeners: Record<string, ((m: unknown) => void)[]> = {}
const command = vi.fn()
const setPermissionMode = vi.fn(() => true)
const sessions = [{ id: 's1', engine: 'codex' }]
vi.mock('@/ws/client', () => ({
  useSessionSocket: () => ({
    attach: vi.fn(), detach: vi.fn(),
    on: (ev: string, cb: (m: unknown) => void) => { (listeners[ev] ||= []).push(cb) },
    off: vi.fn(),
    connectionState: 'connected',
    prompt: vi.fn(), sendInterrupt: vi.fn(), permissionResponse: vi.fn(),
    answerQuestion: vi.fn(), setPermissionMode, cancelQueued: vi.fn(),
    command,
  }),
}))
vi.mock('@/api/hooks', () => ({
  useSessions: () => ({ data: sessions }),
  useAccounts: () => ({ data: [] }),
  useSettings: () => ({ data: { defaultToolDetail: 'auto', thinkingCollapsed: true } }),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: {},
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

import { useSessionRuntime } from '@/session/useSessionRuntime'

// useSessionRuntime invalidates permission-rules after a grant, so it needs a client.
function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client: new QueryClient() }, children)
}

function emit(ev: string, msg: unknown) { (listeners[ev] || []).forEach((cb) => cb(msg)) }

describe('runtime commands', () => {
  beforeEach(() => { for (const k of Object.keys(listeners)) delete listeners[k]; command.mockClear() })

  it('captures catalog and forwards onCommand', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => emit('commands', { type: 'commands', sessionId: 's1', commands: [{ name: 'help', description: '', argumentHint: null, kind: 'client' }] }))
    expect(result.current.commands).toHaveLength(1)
    act(() => result.current.onCommand('model', 'sonnet'))
    expect(command).toHaveBeenCalledWith('s1', 'model', 'sonnet')
  })

  it('onSetModel sends /model now or --after-turn and reflects the choice optimistically', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    expect(result.current.model).toBeNull()          // no record → account default
    expect(result.current.modelPending).toBe(false)
    act(() => result.current.onSetModel('sonnet', 'now'))
    expect(command).toHaveBeenCalledWith('s1', 'model', 'sonnet')
    // The picker shows the REQUESTED alias immediately, not the 5s-polled record.
    expect(result.current.model).toBe('sonnet')
    act(() => result.current.onSetModel('opus', 'after_turn'))
    expect(command).toHaveBeenCalledWith('s1', 'model', 'opus --after-turn')
    expect(result.current.model).toBe('opus')
    // "default" clears the override → null, the wire form of "account default".
    act(() => result.current.onSetModel('default', 'now'))
    expect(command).toHaveBeenCalledWith('s1', 'model', 'default')
    expect(result.current.model).toBeNull()
  })

  it('liveModel tracks the producing model from sessionMeta / turnModel, separate from the picker', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'claude-fable-5' }))
    expect(result.current.liveModel).toBe('claude-fable-5')
    expect(result.current.model).toBeNull()            // the launch model is NOT a user choice
    act(() => emit('sessionEvent', { type: 'turnStart', sessionId: 's1', turnId: 't1' }))
    act(() => emit('sessionEvent', { type: 'turnModel', sessionId: 's1', turnId: 't1', model: 'claude-opus-4-8' }))
    expect(result.current.liveModel).toBe('claude-opus-4-8')
  })

  it('notice result appends to transcript; help result opens overlay', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => emit('commandResult', { type: 'commandResult', sessionId: 's1', ok: true, render: 'notice', data: { text: 'Model → sonnet' } }))
    expect(result.current.state.turns.at(-1)!.blocks[0]).toMatchObject({ kind: 'notice', text: 'Model → sonnet' })
    act(() => emit('commandResult', { type: 'commandResult', sessionId: 's1', ok: true, render: 'help', data: null }))
    expect(result.current.commandResult).toMatchObject({ render: 'help' })
  })
})


describe('runtime effective permission mode', () => {
  beforeEach(() => {
    for (const k of Object.keys(listeners)) delete listeners[k]
    setPermissionMode.mockReset().mockReturnValue(true)
  })

  it('keeps the acknowledged mode until a deferred native change takes effect', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'gpt-5', permissionMode: 'full-auto' }))
    expect(result.current.mode).toBe('full-auto')
    act(() => result.current.onSetMode('plan'))
    expect(setPermissionMode).toHaveBeenCalledWith('s1', 'plan')
    expect(result.current.pendingMode).toBe('plan')
    expect(result.current.mode).toBe('full-auto')
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'gpt-5', permissionMode: 'plan' }))
    expect(result.current.mode).toBe('plan')
    expect(result.current.pendingMode).toBeNull()
    act(() => result.current.onSetMode('default'))
    expect(result.current.mode).toBe('plan')
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'gpt-5', permissionMode: 'full-auto' }))
    expect(result.current.mode).toBe('full-auto')
  })

  it('keeps the latest intent when an earlier acknowledgement arrives', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'gpt-5', permissionMode: 'full-auto' }))
    act(() => result.current.onSetMode('plan'))
    act(() => result.current.onSetMode('default'))
    expect(result.current.pendingMode).toBe('default')
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'gpt-5', permissionMode: 'plan' }))
    expect(result.current.mode).toBe('plan')
    expect(result.current.pendingMode).toBe('default')
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'gpt-5', permissionMode: 'full-auto' }))
    expect(result.current.mode).toBe('full-auto')
    expect(result.current.pendingMode).toBeNull()
  })

  it('does not claim a sent request was applied when native policy refuses it', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => result.current.onSetMode('full-auto'))
    expect(result.current.pendingMode).toBe('full-auto')
    act(() => emit('sessionEvent', { type: 'error', sessionId: 's1', code: 'codex_sandbox_change_refused', message: 'Policy was refused' }))
    expect(result.current.pendingMode).toBeNull()
    expect(result.current.mode).toBe('interactive')
  })

  it('does not create pending intent when the socket rejects the selection', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    setPermissionMode.mockReturnValue(false)
    act(() => result.current.onSetMode('plan'))
    expect(result.current.pendingMode).toBeNull()
    expect(result.current.mode).toBe('interactive')
  })

  it('initializes an idle reattach from the live status snapshot and resets across sessions', () => {
    const { result, rerender } = renderHook(({ id }) => useSessionRuntime(id), { initialProps: { id: 's1' }, wrapper })
    act(() => emit('sessionEvent', { type: 'status', sessionId: 's1', state: 'idle', count: 0, permissionMode: 'full-auto' }))
    expect(result.current.mode).toBe('full-auto')
    rerender({ id: 's2' })
    expect(result.current.mode).toBe('interactive')
    act(() => result.current.onSetMode('full-auto'))
    expect(result.current.mode).toBe('interactive')
  })

  it('ignores another session and legacy metadata without an effective mode', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'), { wrapper })
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'gpt-5', permissionMode: 'full-auto' }))
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's2', model: 'gpt-5', permissionMode: 'plan' }))
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'gpt-5' }))
    expect(result.current.mode).toBe('full-auto')
  })
})
