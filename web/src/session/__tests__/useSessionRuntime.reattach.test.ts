// web/src/session/__tests__/useSessionRuntime.reattach.test.ts
//
// A session viewed while it is still pre-spawn (`pending_approval` / `queued`)
// attached over the WS BEFORE its driver existed — the read-only detached path.
// The server defers the initial prompt "until the first WS attach", so once the
// record leaves that state (approved from this view, or a queue slot freed) the
// view must re-attach: the live attach path is what pops + sends the prompt.
// Without it the prompt is orphaned and the transcript stays empty (2026-09-02).
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'

const attach = vi.fn()
vi.mock('@/ws/client', () => ({
  useSessionSocket: () => ({
    attach, detach: vi.fn(),
    on: vi.fn(), off: vi.fn(),
    connectionState: 'connected',
    prompt: vi.fn(), sendInterrupt: vi.fn(), permissionResponse: vi.fn(),
    answerQuestion: vi.fn(), setPermissionMode: vi.fn(), cancelQueued: vi.fn(),
    command: vi.fn(),
  }),
}))
let sessionsData: Array<{ id: string; status: string }> = []
vi.mock('@/api/hooks', () => ({
  useSessions: () => ({ data: sessionsData }),
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

describe('re-attach when the session leaves a pre-spawn state', () => {
  beforeEach(() => { attach.mockClear(); sessionsData = [] })

  it('re-sends attach once pending_approval → idle (approved from the open view)', () => {
    sessionsData = [{ id: 's1', status: 'pending_approval' }]
    const { rerender } = renderHook(() => useSessionRuntime('s1'))
    expect(attach).toHaveBeenCalledTimes(1)
    sessionsData = [{ id: 's1', status: 'idle' }]
    rerender()
    expect(attach).toHaveBeenCalledTimes(2)
    expect(attach).toHaveBeenLastCalledWith('s1')
  })

  it('re-sends attach once queued → in_progress (a capacity slot freed)', () => {
    sessionsData = [{ id: 's1', status: 'queued' }]
    const { rerender } = renderHook(() => useSessionRuntime('s1'))
    sessionsData = [{ id: 's1', status: 'in_progress' }]
    rerender()
    expect(attach).toHaveBeenCalledTimes(2)
  })

  it('does not re-attach on ordinary live transitions', () => {
    sessionsData = [{ id: 's1', status: 'idle' }]
    const { rerender } = renderHook(() => useSessionRuntime('s1'))
    sessionsData = [{ id: 's1', status: 'in_progress' }]
    rerender()
    sessionsData = [{ id: 's1', status: 'complete' }]
    rerender()
    expect(attach).toHaveBeenCalledTimes(1)
  })

  it('does not re-attach when the record first appears already live', () => {
    const { rerender } = renderHook(() => useSessionRuntime('s1'))
    sessionsData = [{ id: 's1', status: 'idle' }]
    rerender()
    expect(attach).toHaveBeenCalledTimes(1)
  })

  it('does not re-attach for another session\'s transition', () => {
    sessionsData = [{ id: 's1', status: 'idle' }, { id: 's2', status: 'pending_approval' }]
    const { rerender } = renderHook(() => useSessionRuntime('s1'))
    sessionsData = [{ id: 's1', status: 'idle' }, { id: 's2', status: 'idle' }]
    rerender()
    expect(attach).toHaveBeenCalledTimes(1)
  })
})
