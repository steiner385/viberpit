import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import type { NavId } from './navDefs'

// Only `pipeline` is kiosk-safe today, so the real map has nothing to rotate
// through; the hook is exercised over a two-section map.
vi.mock('./navDefs', () => {
  const NAV_BY_ID = {
    pipeline: { id: 'pipeline', path: '/pipeline' },
    x: { id: 'x', path: '/x' },
  }
  return {
    NAV_BY_ID,
    navDefForPath: (p: string) => (p.startsWith('/x') ? NAV_BY_ID.x : p.startsWith('/pipeline') ? NAV_BY_ID.pipeline : null),
  }
})

import { useAttractCycle } from './useAttractCycle'

function Probe({ enabled, pages, cycle }: { enabled: boolean; pages: readonly NavId[]; cycle: number }) {
  useAttractCycle(enabled, pages, cycle)
  const { pathname, search } = useLocation()
  return <div data-testid="loc">{pathname}{search}</div>
}

const PAGES = ['pipeline', 'x'] as unknown as readonly NavId[]

const mount = (props: Partial<React.ComponentProps<typeof Probe>> = {}, entry = '/pipeline?kiosk=1&cycle=10') =>
  render(<MemoryRouter initialEntries={[entry]}><Probe enabled pages={PAGES} cycle={10} {...props} /></MemoryRouter>)

describe('useAttractCycle', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('rotates through the pages every cycleSeconds, wrapping, and keeps the kiosk search', () => {
    mount()
    expect(screen.getByTestId('loc')).toHaveTextContent('/pipeline?kiosk=1&cycle=10')
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(screen.getByTestId('loc')).toHaveTextContent('/x?kiosk=1&cycle=10')
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(screen.getByTestId('loc')).toHaveTextContent('/pipeline?kiosk=1&cycle=10')
  })

  it('does nothing with one page, or when disabled', () => {
    const one = mount({ pages: ['pipeline'] as unknown as readonly NavId[] })
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(screen.getByTestId('loc')).toHaveTextContent('/pipeline')
    one.unmount()
    mount({ enabled: false })
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(screen.getByTestId('loc')).toHaveTextContent('/pipeline')
  })

  it('skips ticks while the document is hidden', () => {
    const spy = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    mount()
    act(() => { vi.advanceTimersByTime(20_000) })
    expect(screen.getByTestId('loc')).toHaveTextContent('/pipeline')
    spy.mockReturnValue(false)
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(screen.getByTestId('loc')).toHaveTextContent('/x')
  })
})
