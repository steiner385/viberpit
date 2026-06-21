import { describe, it, expect, beforeEach, vi } from 'vitest'
import { getLastRepo, setLastRepo } from '../lastRepo'

describe('lastRepo helper', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it('returns null when nothing is stored', () => {
    expect(getLastRepo()).toBeNull()
  })

  it('round-trips a repo nameWithOwner', () => {
    setLastRepo('cairnea/KinDash')
    expect(getLastRepo()).toBe('cairnea/KinDash')
  })

  it('overwrites the previous value', () => {
    setLastRepo('a/one')
    setLastRepo('b/two')
    expect(getLastRepo()).toBe('b/two')
  })

  it('returns null and does not throw when getItem throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(() => getLastRepo()).not.toThrow()
    expect(getLastRepo()).toBeNull()
  })

  it('does not throw when setItem throws (quota / private mode)', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceeded')
    })
    expect(() => setLastRepo('x/y')).not.toThrow()
  })
})
