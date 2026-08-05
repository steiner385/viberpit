import { describe, it, expect } from 'vitest'
import { nextVisibleSessionId, nextSessionAfterClose } from './nextAfterArchive'

describe('nextVisibleSessionId', () => {
  const ids = ['a', 'b', 'c']

  it('returns the row below a middle row', () => {
    expect(nextVisibleSessionId(ids, 'b')).toBe('c')
  })

  it('returns the row below the first row', () => {
    expect(nextVisibleSessionId(ids, 'a')).toBe('b')
  })

  it('returns the row above when removing the last row', () => {
    expect(nextVisibleSessionId(ids, 'c')).toBe('b')
  })

  it('returns null when removing the only row', () => {
    expect(nextVisibleSessionId(['solo'], 'solo')).toBeNull()
  })

  it('returns null when the removed id is not in the visible list', () => {
    expect(nextVisibleSessionId(ids, 'zzz')).toBeNull()
  })

  it('returns null for an empty list', () => {
    expect(nextVisibleSessionId([], 'a')).toBeNull()
  })
})

describe('nextSessionAfterClose', () => {
  // Visible list = what the user sees (collapse/search applied); all = full order.
  const all = ['a', 'b', 'c', 'd']

  it('prefers the visible neighbor when the removed row is visible', () => {
    // 'c' is hidden; the visible neighbor of 'b' is 'd', not 'c'.
    expect(nextSessionAfterClose(['a', 'b', 'd'], all, 'b')).toBe('d')
  })

  it('falls back to the full ordering when the removed session is hidden (collapsed group)', () => {
    // 'b' is in a collapsed group → absent from the visible list.
    expect(nextSessionAfterClose(['a', 'd'], all, 'b')).toBe('c')
  })

  it('falls back to the full ordering when the removed session is the only visible row', () => {
    expect(nextSessionAfterClose(['b'], all, 'b')).toBe('c')
  })

  it('falls back to the full ordering when a search filter hides everything', () => {
    expect(nextSessionAfterClose([], all, 'a')).toBe('b')
  })

  it('returns null when the session is the only one anywhere', () => {
    expect(nextSessionAfterClose(['solo'], ['solo'], 'solo')).toBeNull()
  })

  it('returns null when the session is unknown to both lists', () => {
    expect(nextSessionAfterClose(['a'], all, 'zzz')).toBeNull()
  })
})
