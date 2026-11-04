/**
 * `/btw` (D-12): the dock composer's onCommand opens the Assistant drawer, and with a
 * question also sends it there. Only the catalog's `client` entry is ours (the server
 * lists it in coding sessions, with the Assistant on); anything else goes to the session.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useEffect, type ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router-dom'
import type { Command } from '@/session/events'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { AssistantRequestProvider, usePendingAssistantSend } from '@/shell/assistantGestures'
import { useBtwCommand } from './useBtwCommand'

const status = { data: { enabled: true } as { enabled: boolean } | undefined }
vi.mock('@/api/assistant', () => ({ useAssistantStatus: () => status }))

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
    () => ({ btw: useBtwCommand(rt), pending: usePendingAssistantSend() }), { wrapper })
  return {
    onCommand,
    run: (name: string, args = '') => act(() => result.current.btw.onCommand(name, args)),
    commands: () => result.current.btw.commands.map((c) => c.name),
    pendingSession: () => result.current.pending.pendingSend?.sessionId ?? null,
    drawer: () => new URLSearchParams(search).get('drawer'),
    pending: () => result.current.pending.pendingSend?.text ?? null,
  }
}

describe('useBtwCommand', () => {
  beforeEach(() => { status.data = { enabled: true } })

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
    // Tied to the session it was asked in, so it never goes out about another one.
    expect(t.pendingSession()).toBe('s1')
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

  it('offers /btw in the menu while the Assistant is on, or its status is not known yet', () => {
    expect(setup().commands()).toEqual(['btw'])
    status.data = undefined
    expect(setup().commands()).toEqual(['btw'])
  })

  it('with the Assistant switched off in Settings, /btw is absent: not in the menu, and not ours', () => {
    status.data = { enabled: false }
    const t = setup()
    expect(t.commands()).toEqual([])
    t.run('btw', 'hi')
    expect(t.onCommand).toHaveBeenCalledWith('btw', 'hi')
    expect(t.drawer()).toBeNull()
    expect(t.pending()).toBeNull()
  })
})
