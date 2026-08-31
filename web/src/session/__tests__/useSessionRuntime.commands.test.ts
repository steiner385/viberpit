// web/src/session/__tests__/useSessionRuntime.commands.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

// Mock the socket hook so we can drive inbound commands/commandResult.
const listeners: Record<string, ((m: unknown) => void)[]> = {}
const command = vi.fn()
vi.mock('@/ws/client', () => ({
  useSessionSocket: () => ({
    attach: vi.fn(), detach: vi.fn(),
    on: (ev: string, cb: (m: unknown) => void) => { (listeners[ev] ||= []).push(cb) },
    off: vi.fn(),
    connectionState: 'connected',
    prompt: vi.fn(), sendInterrupt: vi.fn(), permissionResponse: vi.fn(),
    answerQuestion: vi.fn(), setPermissionMode: vi.fn(), cancelQueued: vi.fn(),
    command,
  }),
}))
vi.mock('@/api/hooks', () => ({
  useSessions: () => ({ data: [] }),
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

function emit(ev: string, msg: unknown) { (listeners[ev] || []).forEach((cb) => cb(msg)) }

describe('runtime commands', () => {
  beforeEach(() => { for (const k of Object.keys(listeners)) delete listeners[k]; command.mockClear() })

  it('captures catalog and forwards onCommand', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'))
    act(() => emit('commands', { type: 'commands', sessionId: 's1', commands: [{ name: 'help', description: '', argumentHint: null, kind: 'client' }] }))
    expect(result.current.commands).toHaveLength(1)
    act(() => result.current.onCommand('model', 'sonnet'))
    expect(command).toHaveBeenCalledWith('s1', 'model', 'sonnet')
  })

  it('onSetModel sends /model now or --after-turn and reflects the choice optimistically', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'))
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
    const { result } = renderHook(() => useSessionRuntime('s1'))
    act(() => emit('sessionEvent', { type: 'sessionMeta', sessionId: 's1', model: 'claude-fable-5' }))
    expect(result.current.liveModel).toBe('claude-fable-5')
    expect(result.current.model).toBeNull()            // the launch model is NOT a user choice
    act(() => emit('sessionEvent', { type: 'turnStart', sessionId: 's1', turnId: 't1' }))
    act(() => emit('sessionEvent', { type: 'turnModel', sessionId: 's1', turnId: 't1', model: 'claude-opus-4-8' }))
    expect(result.current.liveModel).toBe('claude-opus-4-8')
  })

  it('notice result appends to transcript; help result opens overlay', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'))
    act(() => emit('commandResult', { type: 'commandResult', sessionId: 's1', ok: true, render: 'notice', data: { text: 'Model → sonnet' } }))
    expect(result.current.state.turns.at(-1)!.blocks[0]).toMatchObject({ kind: 'notice', text: 'Model → sonnet' })
    act(() => emit('commandResult', { type: 'commandResult', sessionId: 's1', ok: true, render: 'help', data: null }))
    expect(result.current.commandResult).toMatchObject({ render: 'help' })
  })
})
