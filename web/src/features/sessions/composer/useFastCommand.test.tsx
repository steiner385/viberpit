/**
 * `/fast` (D-14): the catalog lists it as a `client` command, so the browser runs it —
 * the same `POST /sessions/{id}/fast` the ⚡ toggle posts. Without this the server's
 * dispatcher only answers with a "send it from the message box" notice.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { Command } from '@/session/events'
import type { Session } from '@/api/types'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { session } from '@/test/factories'
import { useFastCommand } from './useFastCommand'

const mutate = vi.fn()
let SESSIONS: Session[] = []
vi.mock('@/api/hooks', () => ({
  useSessions: () => ({ data: SESSIONS }),
  useSetFastMode: () => ({ mutate }),
}))

const FAST: Command = { name: 'fast', description: '', argumentHint: '[on|off]', kind: 'client', source: 'app' }

function setup(commands: Command[] = [FAST]) {
  const onCommand = vi.fn()
  const rt = { sessionId: 's1', commands, onCommand } as unknown as SessionRuntime
  const { result } = renderHook(() => useFastCommand(rt))
  return { onCommand, run: (name: string, args = '') => act(() => result.current(name, args)) }
}

describe('useFastCommand', () => {
  beforeEach(() => {
    mutate.mockClear()
    SESSIONS = [session({ id: 's1', fastMode: false, fastModeAvailable: true })]
  })

  it('bare /fast toggles the stored choice', () => {
    const t = setup()
    t.run('fast')
    expect(mutate).toHaveBeenCalledWith({ id: 's1', enabled: true })
    SESSIONS = [session({ id: 's1', fastMode: true, fastModeAvailable: true })]
    const t2 = setup()
    t2.run('fast')
    expect(mutate).toHaveBeenLastCalledWith({ id: 's1', enabled: false })
    expect(t.onCommand).not.toHaveBeenCalled()
  })

  it('/fast on and /fast off set it explicitly', () => {
    const t = setup()
    t.run('fast', 'on')
    expect(mutate).toHaveBeenLastCalledWith({ id: 's1', enabled: true })
    t.run('fast', ' OFF ')
    expect(mutate).toHaveBeenLastCalledWith({ id: 's1', enabled: false })
  })

  it('anything that is not our client /fast goes on to the session', () => {
    const t = setup([])
    t.run('fast', 'on')
    t.run('model', 'opus')
    expect(mutate).not.toHaveBeenCalled()
    expect(t.onCommand).toHaveBeenCalledWith('fast', 'on')
    expect(t.onCommand).toHaveBeenCalledWith('model', 'opus')
  })
})
