import { describe, it, expect, beforeEach } from 'vitest'
import {
  SIDEBAR_MIN,
  SIDEBAR_MAX,
  SIDEBAR_DEFAULT,
  clampSidebarWidth,
  loadSidebarWidth,
  saveSidebarWidth,
  SIDEBAR_STORAGE_KEY,
} from '../sidebarWidth'

describe('clampSidebarWidth', () => {
  it('passes through an in-range width', () => {
    expect(clampSidebarWidth(400)).toBe(400)
  })

  it('clamps below the minimum', () => {
    expect(clampSidebarWidth(SIDEBAR_MIN - 50)).toBe(SIDEBAR_MIN)
  })

  it('clamps above the maximum', () => {
    expect(clampSidebarWidth(SIDEBAR_MAX + 50)).toBe(SIDEBAR_MAX)
  })

  it('rounds fractional widths', () => {
    expect(clampSidebarWidth(400.7)).toBe(401)
  })

  it('falls back to the default for non-finite input', () => {
    expect(clampSidebarWidth(NaN)).toBe(SIDEBAR_DEFAULT)
    expect(clampSidebarWidth(Infinity)).toBe(SIDEBAR_MAX)
  })
})

describe('loadSidebarWidth / saveSidebarWidth', () => {
  beforeEach(() => localStorage.clear())

  it('returns the default when nothing is stored', () => {
    expect(loadSidebarWidth()).toBe(SIDEBAR_DEFAULT)
  })

  it('round-trips a saved width', () => {
    saveSidebarWidth(420)
    expect(loadSidebarWidth()).toBe(420)
  })

  it('clamps a stored value that is out of range', () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, String(SIDEBAR_MAX + 999))
    expect(loadSidebarWidth()).toBe(SIDEBAR_MAX)
  })

  it('returns the default for a corrupt stored value', () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, 'not-a-number')
    expect(loadSidebarWidth()).toBe(SIDEBAR_DEFAULT)
  })
})
