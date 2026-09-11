// Ported from plotroom@5166b52 frontend/src/__tests__/PipelineSwitcher.test.tsx (the
// hook cases; the picker component's cases went with the component when the
// page's Tabs subnav replaced it in finish-line row 5).
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useFocusedPipeline } from './useFocusedPipeline'

const REPOS = ['cairnea/KinDash', 'cairnea/infra', 'steiner385/pr-dashboard']

describe('useFocusedPipeline (sticky focus)', () => {
  beforeEach(() => { localStorage.clear(); history.replaceState(null, '', '/') })
  it('defaults to the first repo when nothing stored', () => {
    const { result } = renderHook(() => useFocusedPipeline(REPOS))
    expect(result.current[0]).toBe('cairnea/KinDash')
  })
  it('persists a focus choice to localStorage', () => {
    const { result } = renderHook(() => useFocusedPipeline(REPOS))
    act(() => result.current[1]('cairnea/infra'))
    expect(result.current[0]).toBe('cairnea/infra')
    expect(localStorage.getItem('workspace.focusedPipeline')).toBe('cairnea/infra')
  })
  it('restores a persisted focus on mount', () => {
    localStorage.setItem('workspace.focusedPipeline', 'steiner385/pr-dashboard')
    const { result } = renderHook(() => useFocusedPipeline(REPOS))
    expect(result.current[0]).toBe('steiner385/pr-dashboard')
  })
  it('a ?pipeline= deep link wins over localStorage (#191)', () => {
    localStorage.setItem('workspace.focusedPipeline', 'cairnea/KinDash')
    history.replaceState(null, '', '/?pipeline=cairnea%2Finfra')
    const { result } = renderHook(() => useFocusedPipeline(REPOS))
    expect(result.current[0]).toBe('cairnea/infra')
  })
  it('focus() mirrors the choice into the ?pipeline= param (#191)', () => {
    const { result } = renderHook(() => useFocusedPipeline(REPOS))
    act(() => result.current[1]('cairnea/infra'))
    expect(new URLSearchParams(location.search).get('pipeline')).toBe('cairnea/infra')
  })
  it('useUrl=false ignores the URL param (host opts out)', () => {
    localStorage.setItem('workspace.focusedPipeline', 'cairnea/KinDash')
    history.replaceState(null, '', '/?pipeline=cairnea%2Finfra')
    const { result } = renderHook(() => useFocusedPipeline(REPOS, true, false))
    expect(result.current[0]).toBe('cairnea/KinDash') // URL ignored → localStorage
  })
  it('adopts the first repo when repos arrive AFTER mount (live-browser bug regression)', () => {
    const { result, rerender } = renderHook(({ repos }) => useFocusedPipeline(repos), { initialProps: { repos: [] as string[] } })
    expect(result.current[0]).toBeNull()
    act(() => rerender({ repos: REPOS }))
    expect(result.current[0]).toBe('cairnea/KinDash')
  })
  it('adopts a persisted focus (not just repos[0]) when repos arrive after mount', () => {
    localStorage.setItem('workspace.focusedPipeline', 'cairnea/infra')
    const { result, rerender } = renderHook(({ repos }) => useFocusedPipeline(repos), { initialProps: { repos: [] as string[] } })
    act(() => rerender({ repos: REPOS }))
    expect(result.current[0]).toBe('cairnea/infra')
  })
})
