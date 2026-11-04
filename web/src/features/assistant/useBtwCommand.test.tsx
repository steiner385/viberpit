/**
 * `/btw` (D-12): the dock composer's onCommand opens the Assistant drawer, and with a
 * question also sends it there. Only the catalog's `client` entry is ours (the server
 * lists it in coding sessions, with the Assistant on); anything else goes to the session.
 */
import { describe, it, expect, vi } from 'vitest'
import { useEffect, type ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router-dom'
import type { Command } from '@/session/events'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { AssistantRequestProvider, usePendingAssistantSend } from '@/shell/assistantGestures'
import { useBtwCommand } from './useBtwCommand'

const BTW: Command = { name: 'btw', description: '', argumentHint: '<question>', kind: 'client', source: 'app' }

function setup(commands: Command[] = [BTW]) {
  const onCommand = vi.fn()
  const rt = { sessionId: 's1', commands, onCommand } as unknown as SessionRuntime
  let search = ''
  function Probe({ children }: { children: ReactNode }) {
    const { search: current } = useLocation()
    useEffect(() => { search = current }, [current])
    return <>{children}</>
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <RouterProvider router={createMemoryRouter(
      [{ path: '*', element: <AssistantRequestProvider><Probe>{children}</Probe></AssistantRequestProvider> }],
      { initialEntries: ['/sessions/s1'] },
    )} />
  )
  const { result } = renderHook(
    () => ({ run: useBtwCommand(rt), pending: usePendingAssistantSend() }), { wrapper })
  return {
    onCommand,
    run: (name: string, args = '') => act(() => result.current.run(name, args)),
    drawer: () => new URLSearchParams(search).get('drawer'),
    pending: () => result.current.pending.pendingSend?.text ?? null,
  }
}

describe('useBtwCommand', () => {
  it('/btw alone opens the drawer and sends nothing', () => {
    const t = setup()
    t.run('btw', '')
    expect(t.drawer()).toBe('assistant')
    expect(t.pending()).toBeNull()
    expect(t.onCommand).not.toHaveBeenCalled()
  })

  it('/btw <question> opens the drawer and parks the question for it to send', () => {
    const t = setup()
    t.run('btw', '  why is it failing ')
    expect(t.drawer()).toBe('assistant')
    expect(t.pending()).toBe('why is it failing')
    expect(t.onCommand).not.toHaveBeenCalled()
  })

  it('any other command goes to the session', () => {
    const t = setup()
    t.run('compact', 'now')
    expect(t.onCommand).toHaveBeenCalledWith('compact', 'now')
    expect(t.drawer()).toBeNull()
  })

  it('a btw that is not our client entry goes to the session', () => {
    const t = setup([{ ...BTW, kind: 'passthrough', source: 'vendor' }])
    t.run('btw', 'hi')
    expect(t.onCommand).toHaveBeenCalledWith('btw', 'hi')
    expect(t.drawer()).toBeNull()
    expect(t.pending()).toBeNull()
  })

  it('with the Assistant off the catalog omits /btw, so it goes to the session untouched', () => {
    const t = setup([])
    t.run('btw', 'hi')
    expect(t.onCommand).toHaveBeenCalledWith('btw', 'hi')
    expect(t.drawer()).toBeNull()
  })
})
