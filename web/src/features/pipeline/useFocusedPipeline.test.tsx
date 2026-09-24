// Ported from plotroom@5166b52 frontend/src/__tests__/PipelineSwitcher.test.tsx (the
// hook cases; the picker component's cases went with the component when the
// page's Tabs subnav replaced it in finish-line row 5). Row 5 review residual
// #1555 — reads/writes go through react-router's useSearchParams (not raw
// history.replaceState), so the hook needs a Router in scope; a combined
// probe hook (mirroring lib/useSearchParamState.test.tsx) exposes useLocation()
// alongside the hook under test so a write can be asserted from the router's
// own location, not the real `window.location` (a MemoryRouter never touches it).
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { storageKey } from '@/lib/storageKey'
import { useFocusedPipeline } from './useFocusedPipeline'

const REPOS = ['cairnea/KinDash', 'cairnea/infra', 'steiner385/pr-dashboard']
const STORE_KEY = storageKey('pipeline.focused')
const LEGACY_STORE_KEY = 'workspace.focusedPipeline'

function wrap(initial: string) {
  return ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
}

/** Combines the hook under test with `useLocation()` so a write can be
 *  asserted against the router's own location. */
function useProbe(repos: readonly string[], enabled?: boolean, persist?: boolean) {
  return { pipeline: useFocusedPipeline(repos, enabled, persist), loc: useLocation() }
}

describe('useFocusedPipeline (sticky focus)', () => {
  beforeEach(() => { localStorage.clear() })
  it('defaults to the first repo when nothing stored', () => {
    const { result } = renderHook(() => useFocusedPipeline(REPOS), { wrapper: wrap('/pipeline') })
    expect(result.current[0]).toBe('cairnea/KinDash')
  })
  it('persists a focus choice to localStorage', () => {
    const { result } = renderHook(() => useFocusedPipeline(REPOS), { wrapper: wrap('/pipeline') })
    act(() => result.current[1]('cairnea/infra'))
    expect(result.current[0]).toBe('cairnea/infra')
    expect(localStorage.getItem(STORE_KEY)).toBe('cairnea/infra')
  })
  it('restores a persisted focus on mount', () => {
    localStorage.setItem(STORE_KEY, 'steiner385/pr-dashboard')
    const { result } = renderHook(() => useFocusedPipeline(REPOS), { wrapper: wrap('/pipeline') })
    expect(result.current[0]).toBe('steiner385/pr-dashboard')
  })
  it('a ?pipeline= deep link wins over localStorage (#191)', () => {
    localStorage.setItem(STORE_KEY, 'cairnea/KinDash')
    const { result } = renderHook(() => useFocusedPipeline(REPOS), { wrapper: wrap('/pipeline?pipeline=cairnea%2Finfra') })
    expect(result.current[0]).toBe('cairnea/infra')
  })
  it('focus() mirrors the choice into the router\'s ?pipeline= param (#191, #1555)', () => {
    const { result } = renderHook(() => useProbe(REPOS), { wrapper: wrap('/pipeline') })
    act(() => result.current.pipeline[1]('cairnea/infra'))
    expect(new URLSearchParams(result.current.loc.search).get('pipeline')).toBe('cairnea/infra')
  })
  it('persist=false (kiosk) still reads a ?pipeline= deep link (#1555) but never writes URL or localStorage', () => {
    localStorage.setItem(STORE_KEY, 'cairnea/KinDash')
    const { result } = renderHook(() => useProbe(REPOS, true, false), { wrapper: wrap('/pipeline?pipeline=cairnea%2Finfra') })
    // The kiosk's own deep link wins on mount — it must not silently fall back to repos[0].
    expect(result.current.pipeline[0]).toBe('cairnea/infra')
    act(() => result.current.pipeline[1]('steiner385/pr-dashboard'))
    // The auto-cycle's focus() changed local state...
    expect(result.current.pipeline[0]).toBe('steiner385/pr-dashboard')
    // ...but never touched the persisted choice or the display's configured URL.
    expect(localStorage.getItem(STORE_KEY)).toBe('cairnea/KinDash')
    expect(new URLSearchParams(result.current.loc.search).get('pipeline')).toBe('cairnea/infra')
  })
  it('adopts the first repo when repos arrive AFTER mount (live-browser bug regression)', () => {
    const { result, rerender } = renderHook(({ repos }) => useFocusedPipeline(repos), { wrapper: wrap('/pipeline'), initialProps: { repos: [] as string[] } })
    expect(result.current[0]).toBeNull()
    act(() => rerender({ repos: REPOS }))
    expect(result.current[0]).toBe('cairnea/KinDash')
  })
  it('adopts a persisted focus (not just repos[0]) when repos arrive after mount', () => {
    localStorage.setItem(STORE_KEY, 'cairnea/infra')
    const { result, rerender } = renderHook(({ repos }) => useFocusedPipeline(repos), { wrapper: wrap('/pipeline'), initialProps: { repos: [] as string[] } })
    act(() => rerender({ repos: REPOS }))
    expect(result.current[0]).toBe('cairnea/infra')
  })
  it('migrates the legacy workspace.focusedPipeline key once', () => {
    localStorage.setItem(LEGACY_STORE_KEY, 'cairnea/infra')
    const { result } = renderHook(() => useFocusedPipeline(REPOS), { wrapper: wrap('/pipeline') })
    expect(result.current[0]).toBe('cairnea/infra')
    expect(localStorage.getItem(STORE_KEY)).toBe('cairnea/infra')
    expect(localStorage.getItem(LEGACY_STORE_KEY)).toBeNull()
  })
})
