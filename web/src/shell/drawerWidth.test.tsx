/**
 * R15: the drawer width is ONE store, not a copy per component — `DrawerHost`
 * (the open drawer) and Settings › Appearance both hold it, so a Reset in
 * Settings has to move the live drawer without a reload.
 */
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import {
  useDrawerWidth,
  writeDrawerWidth,
  clampDrawerWidth,
  DRAWER_DEFAULT,
  DRAWER_MAX,
  DRAWER_STORAGE_KEY,
} from './drawerWidth'

describe('clampDrawerWidth', () => {
  it('clamps to the band and falls back to the default for non-finite input', () => {
    expect(clampDrawerWidth(500)).toBe(500)
    expect(clampDrawerWidth(DRAWER_MAX + 100)).toBe(DRAWER_MAX)
    expect(clampDrawerWidth(NaN)).toBe(DRAWER_DEFAULT)
  })
})

describe('useDrawerWidth', () => {
  it('starts at the stored width and every subscriber sees one write', () => {
    localStorage.setItem(DRAWER_STORAGE_KEY, '520')
    const a = renderHook(() => useDrawerWidth())
    const b = renderHook(() => useDrawerWidth())
    expect(a.result.current[0]).toBe(520)

    act(() => a.result.current[1](DRAWER_DEFAULT))
    expect(a.result.current[0]).toBe(DRAWER_DEFAULT)
    expect(b.result.current[0]).toBe(DRAWER_DEFAULT)
    expect(localStorage.getItem(DRAWER_STORAGE_KEY)).toBe(String(DRAWER_DEFAULT))
  })

  it('clamps what the setter is given, and notifies on a write made outside React', () => {
    const { result } = renderHook(() => useDrawerWidth())
    act(() => result.current[1](DRAWER_MAX + 200))
    expect(result.current[0]).toBe(DRAWER_MAX)
    act(() => writeDrawerWidth(400))
    expect(result.current[0]).toBe(400)
  })
})
