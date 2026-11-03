import { describe, it, expect } from 'vitest'
import { stepSessionId } from './listStep'

const IDS = ['a', 'b', 'c']

describe('stepSessionId — Alt+[ / Alt+] and the header chevrons', () => {
  it('moves one row through the list order', () => {
    expect(stepSessionId(IDS, 'b', 'next')).toBe('c')
    expect(stepSessionId(IDS, 'b', 'prev')).toBe('a')
  })

  it('wraps around both ends', () => {
    expect(stepSessionId(IDS, 'c', 'next')).toBe('a')
    expect(stepSessionId(IDS, 'a', 'prev')).toBe('c')
  })

  it('with no session open, next is the first row and prev the last', () => {
    expect(stepSessionId(IDS, null, 'next')).toBe('a')
    expect(stepSessionId(IDS, null, 'prev')).toBe('c')
  })

  it('an open session the list does not show (filtered, collapsed) steps like no session open', () => {
    expect(stepSessionId(IDS, 'z', 'next')).toBe('a')
    expect(stepSessionId(IDS, 'z', 'prev')).toBe('c')
  })

  it('has nowhere to go when the open session is the only row, or the list is empty', () => {
    expect(stepSessionId(['a'], 'a', 'next')).toBeNull()
    expect(stepSessionId(['a'], 'a', 'prev')).toBeNull()
    expect(stepSessionId([], null, 'next')).toBeNull()
    expect(stepSessionId([], 'a', 'prev')).toBeNull()
  })

  it('a lone row that is not the open one is still a destination', () => {
    expect(stepSessionId(['a'], null, 'next')).toBe('a')
    expect(stepSessionId(['a'], 'z', 'prev')).toBe('a')
  })
})
