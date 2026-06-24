import { describe, it, expect, beforeEach } from 'vitest'
import { loadGroupCollapse, saveGroupCollapse } from '../groupCollapse'

describe('groupCollapse', () => {
  beforeEach(() => window.localStorage.clear())

  it('defaults to an empty set when nothing is stored', () => {
    expect(loadGroupCollapse()).toEqual(new Set())
  })

  it('round-trips a set of keys', () => {
    saveGroupCollapse(new Set(['busy', 'Scratch']))
    expect(loadGroupCollapse()).toEqual(new Set(['busy', 'Scratch']))
  })

  it('serializes to a JSON string array', () => {
    saveGroupCollapse(new Set(['busy']))
    expect(window.localStorage.getItem('ccswitch.groupCollapse')).toBe('["busy"]')
  })

  it('falls back to an empty set on malformed JSON', () => {
    window.localStorage.setItem('ccswitch.groupCollapse', '{not json')
    expect(loadGroupCollapse()).toEqual(new Set())
  })

  it('falls back to an empty set when the payload is not an array', () => {
    window.localStorage.setItem('ccswitch.groupCollapse', '{"a":1}')
    expect(loadGroupCollapse()).toEqual(new Set())
  })

  it('ignores non-string array entries', () => {
    window.localStorage.setItem('ccswitch.groupCollapse', '["busy",1,null]')
    expect(loadGroupCollapse()).toEqual(new Set(['busy']))
  })
})
