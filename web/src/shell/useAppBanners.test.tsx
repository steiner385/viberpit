import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'

vi.mock('@/hooks/useInputHold', () => ({ useInputHold: vi.fn() }))

import { useInputHold } from '@/hooks/useInputHold'
import { useAppBanners } from './useAppBanners'

const NONE = { allLimitedReset: undefined, onDismissAllLimited: () => {} }

describe('useAppBanners', () => {
  beforeEach(() => {
    vi.mocked(useInputHold).mockReturnValue({ holding: false, count: 0, mode: 'updating' })
  })

  it('declares every banner exactly once', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    const ids = result.current.map((i) => i.id)
    expect(new Set(ids).size).toBe(ids.length)
    expect(ids.sort()).toEqual(
      ['all-limited', 'daemon', 'deploy', 'git-credential', 'input-hold', 'update'].sort(),
    )
  })

  it('preserves the legacy priorities', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    const byId = Object.fromEntries(result.current.map((i) => [i.id, i.priority]))
    expect(byId).toEqual({
      daemon: 90, 'git-credential': 80, 'all-limited': 70,
      'input-hold': 60, deploy: 50, update: 30,
    })
  })

  it('assigns a tone to every item', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    for (const i of result.current) expect(i.tone, i.id).toBeDefined()
    const byId = Object.fromEntries(result.current.map((i) => [i.id, i.tone]))
    expect(byId.daemon).toBe('danger')
    expect(byId['all-limited']).toBe('danger')
    expect(byId['git-credential']).toBe('warning')
  })

  it('includes input-hold — the banner that was mobile-only before this PR', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    expect(result.current.some((i) => i.id === 'input-hold')).toBe(true)
  })

  // The deploy panel is the tallest banner in the stack — a title row, a CI
  // reason, a Deploy button, and an expandable commit list — and a phone has
  // the least room to spare. It moves to Settings › Deploy there.
  it('hides only the deploy banner on phone', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    const phoneHidden = result.current.filter((i) => i.hideOn?.includes('phone')).map((i) => i.id)
    expect(phoneHidden).toEqual(['deploy'])
  })

  it('keeps the deploy banner on tablet, desktop and kiosk', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    const deploy = result.current.find((i) => i.id === 'deploy')
    expect(deploy?.hideOn).toEqual(['phone'])
  })

  it('renders no all-limited node until a reset time is supplied', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    expect(result.current.find((i) => i.id === 'all-limited')?.node).toBeNull()
  })

  it('renders the input-hold node once useInputHold reports holding', () => {
    vi.mocked(useInputHold).mockReturnValue({ holding: true, count: 2, mode: 'reconnecting' })
    const { result } = renderHook(() => useAppBanners(NONE))
    expect(result.current.find((i) => i.id === 'input-hold')?.node).not.toBeNull()
  })
})
