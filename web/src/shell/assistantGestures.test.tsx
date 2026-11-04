/**
 * The open-and-send channel (D-12): `requestAssistant` opens the Assistant drawer and
 * parks an optional send; the sheet takes it exactly once. URL parameters never drive it.
 */
import { describe, it, expect } from 'vitest'
import { useEffect, type ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router-dom'
import {
  AssistantRequestProvider, useAssistantSentScope, usePendingAssistantSend, useRequestAssistant,
  type PendingAssistantSend,
} from './assistantGestures'
import { useCloseDrawer, useOpenDrawer } from './drawerParam'

function harness(path = '/sessions/s1') {
  let search = ''
  function Probe({ children }: { children: ReactNode }) {
    const { search: current } = useLocation()
    useEffect(() => { search = current }, [current])
    return <>{children}</>
  }
  return {
    search: () => search,
    wrapper: ({ children }: { children: ReactNode }) => {
      const router = createMemoryRouter(
        [{ path: '*', element: <AssistantRequestProvider><Probe>{children}</Probe></AssistantRequestProvider> }],
        { initialEntries: [path] },
      )
      return <RouterProvider router={router} />
    },
  }
}

function useBoth() {
  return {
    request: useRequestAssistant(), pending: usePendingAssistantSend(),
    close: useCloseDrawer(), open: useOpenDrawer(), scope: useAssistantSentScope(),
  }
}

describe('assistantGestures — the open-and-send channel', () => {
  it('requestAssistant() opens the Assistant drawer and parks nothing', () => {
    const h = harness()
    const { result } = renderHook(useBoth, { wrapper: h.wrapper })
    act(() => result.current.request())
    expect(new URLSearchParams(h.search()).get('drawer')).toBe('assistant')
    expect(result.current.pending.pendingSend).toBeNull()
  })

  it('requestAssistant({ send }) opens the drawer and parks the text, tied to its session, until it is taken, once', () => {
    const h = harness()
    const { result } = renderHook(useBoth, { wrapper: h.wrapper })
    act(() => result.current.request({ send: 'why is it failing', sessionId: 's1' }))
    expect(new URLSearchParams(h.search()).get('drawer')).toBe('assistant')
    expect(result.current.pending.pendingSend).toMatchObject({ text: 'why is it failing', sessionId: 's1' })
    let first: PendingAssistantSend | null = null
    let second: PendingAssistantSend | null | 'unset' = 'unset'
    act(() => { first = result.current.pending.take() })
    act(() => { second = result.current.pending.take() })
    expect(first).toMatchObject({ text: 'why is it failing', sessionId: 's1' })
    expect(second).toBeNull()
    expect(result.current.pending.pendingSend).toBeNull()
  })

  it('a blank send parks nothing', () => {
    const h = harness()
    const { result } = renderHook(useBoth, { wrapper: h.wrapper })
    act(() => result.current.request({ send: '   ', sessionId: 's1' }))
    expect(result.current.pending.pendingSend).toBeNull()
  })

  it('the same text twice is two requests (a fresh identity each time)', () => {
    const h = harness()
    const { result } = renderHook(useBoth, { wrapper: h.wrapper })
    act(() => result.current.request({ send: 'again', sessionId: 's1' }))
    const a = result.current.pending.pendingSend
    act(() => { result.current.pending.take() })
    act(() => result.current.request({ send: 'again', sessionId: 's1' }))
    expect(result.current.pending.pendingSend).not.toBe(a)
    expect(result.current.pending.pendingSend?.text).toBe('again')
  })

  it('URL parameters never park a send', () => {
    const h = harness('/sessions/s1?drawer=assistant&send=hello&btw=hello&prompt=hello')
    const { result } = renderHook(useBoth, { wrapper: h.wrapper })
    expect(result.current.pending.pendingSend).toBeNull()
  })

  it('closing the Assistant drops a parked send', () => {
    const h = harness()
    const { result } = renderHook(useBoth, { wrapper: h.wrapper })
    act(() => result.current.request({ send: 'why', sessionId: 's1' }))
    act(() => result.current.close())
    expect(result.current.pending.pendingSend).toBeNull()
  })

  it('opening Help over the Assistant, and closing Help, keeps a parked send', () => {
    const h = harness()
    const { result } = renderHook(useBoth, { wrapper: h.wrapper })
    act(() => result.current.request({ send: 'why', sessionId: 's1' }))
    act(() => result.current.open('help'))
    expect(new URLSearchParams(h.search()).get('drawer')).toBe('help')
    act(() => result.current.close())
    expect(result.current.pending.pendingSend?.text).toBe('why')
  })

  it('records the session the drawer last sent a context about', () => {
    const h = harness()
    const { result } = renderHook(useBoth, { wrapper: h.wrapper })
    expect(result.current.scope.sentScope).toBeUndefined()
    act(() => result.current.scope.recordSentScope('s1'))
    expect(result.current.scope.sentScope).toBe('s1')
    act(() => result.current.scope.recordSentScope(null))
    expect(result.current.scope.sentScope).toBeNull()
  })

  it('outside the provider the reader is inert rather than throwing', () => {
    const { result } = renderHook(usePendingAssistantSend)
    expect(result.current.pendingSend).toBeNull()
    expect(result.current.take()).toBeNull()
  })
})
