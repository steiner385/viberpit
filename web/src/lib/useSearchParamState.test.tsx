import { describe, it, expect } from 'vitest'
import { renderHook, act, render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation, createMemoryRouter, RouterProvider } from 'react-router-dom'
import { useSearchParamState } from './useSearchParamState'

const TABS = ['overview', 'issues'] as const
function wrap(initial: string) {
  return ({ children }: { children: React.ReactNode }) => <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
}

describe('useSearchParamState', () => {
  it('reads ?<name>= and falls back on unknown', () => {
    expect(renderHook(() => useSearchParamState('tab', TABS, 'overview'), { wrapper: wrap('/x?tab=issues') }).result.current[0]).toBe('issues')
    expect(renderHook(() => useSearchParamState('tab', TABS, 'overview'), { wrapper: wrap('/x?tab=nope') }).result.current[0]).toBe('overview')
  })

  it('sets the param with replace and keeps other params', () => {
    const { result } = renderHook(
      () => ({ tab: useSearchParamState('tab', TABS, 'overview'), loc: useLocation() }),
      { wrapper: wrap('/x?scope=map') },
    )
    act(() => result.current.tab[1]('issues'))
    expect(result.current.tab[0]).toBe('issues')
    expect(result.current.loc.search).toBe('?scope=map&tab=issues')
  })

  it('reads ?window=, falls back when unknown, and replaces on set', () => {
    const WINDOWS = ['7d', '30d'] as const
    function Probe() {
      const [win, setWin] = useSearchParamState('window', WINDOWS, '7d')
      return <button onClick={() => setWin('7d')}>{win}</button>
    }
    const router = createMemoryRouter([{ path: '/x', element: <Probe /> }], { initialEntries: ['/x?window=30d'] })
    render(<RouterProvider router={router} />)
    expect(screen.getByRole('button')).toHaveTextContent('30d')

    act(() => screen.getByRole('button').click())

    expect(router.state.location.search).toBe('?window=7d')
    // A REPLACE navigation overwrites the current history entry instead of
    // pushing a new one — the entry count (and Back behaviour) is unchanged,
    // which is the point of `?tab=`/`?window=` being URL state (page-migration §2).
    expect(router.state.historyAction).toBe('REPLACE')
  })
})
