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
  QK: {},
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

  it('notice result appends to transcript; help result opens overlay', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'))
    act(() => emit('commandResult', { type: 'commandResult', sessionId: 's1', ok: true, render: 'notice', data: { text: 'Model → sonnet' } }))
    expect(result.current.state.turns.at(-1)!.blocks[0]).toMatchObject({ kind: 'notice', text: 'Model → sonnet' })
    act(() => emit('commandResult', { type: 'commandResult', sessionId: 's1', ok: true, render: 'help', data: null }))
    expect(result.current.commandResult).toMatchObject({ render: 'help' })
  })
})
