import { describe, it, expect, beforeEach, vi } from 'vitest'
import { getDispatchToggle, setDispatchToggle } from '../dispatchToggles'

describe('dispatchToggles helper', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it('defaults both toggles ON when nothing is stored', () => {
    expect(getDispatchToggle('autopilot')).toBe(true)
    expect(getDispatchToggle('autoArchive')).toBe(true)
  })

  it('round-trips each toggle independently', () => {
    setDispatchToggle('autopilot', false)
    expect(getDispatchToggle('autopilot')).toBe(false)
    expect(getDispatchToggle('autoArchive')).toBe(true)
    setDispatchToggle('autopilot', true)
    expect(getDispatchToggle('autopilot')).toBe(true)
  })

  it('treats an unrecognised stored value as the default (ON)', () => {
    window.localStorage.setItem('viberpit.dispatch.autoArchive', 'garbage')
    expect(getDispatchToggle('autoArchive')).toBe(true)
  })

  it('returns the default and does not throw when getItem throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(getDispatchToggle('autopilot')).toBe(true)
  })

  it('does not throw when setItem throws (quota / private mode)', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceeded')
    })
    expect(() => setDispatchToggle('autopilot', false)).not.toThrow()
  })
})
