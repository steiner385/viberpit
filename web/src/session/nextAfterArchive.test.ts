import { describe, it, expect } from 'vitest'
import { nextVisibleSessionId } from './nextAfterArchive'

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
