import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { useTabParam } from './useTabParam'

const TABS = ['overview', 'issues'] as const
function wrap(initial: string) {
  return ({ children }: { children: React.ReactNode }) => <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
}

describe('useTabParam', () => {
  it('reads ?tab= and falls back on unknown', () => {
    expect(renderHook(() => useTabParam(TABS, 'overview'), { wrapper: wrap('/x?tab=issues') }).result.current[0]).toBe('issues')
    expect(renderHook(() => useTabParam(TABS, 'overview'), { wrapper: wrap('/x?tab=nope') }).result.current[0]).toBe('overview')
  })
  it('sets the param with replace and keeps other params', () => {
    const { result } = renderHook(() => ({ tab: useTabParam(TABS, 'overview'), loc: useLocation() }), { wrapper: wrap('/x?scope=map') })
    act(() => result.current.tab[1]('issues'))
    expect(result.current.tab[0]).toBe('issues')
    expect(result.current.loc.search).toBe('?scope=map&tab=issues')
  })
})
