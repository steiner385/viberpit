// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { spliceAt } from '../dictationInsert'

describe('spliceAt', () => {
  it('inserts at the anchor when nothing is removed', () => {
    expect(spliceAt('hi world', 2, 0, ' there')).toBe('hi there world')
  })

  it('replaces a previously-inserted interim chunk', () => {
    // "hi |world" with a 5-char interim "hello" already at anchor 3 → replace with "hello world"
    expect(spliceAt('hi hello', 3, 5, 'hello world')).toBe('hi hello world')
  })

  it('inserts at the end when anchor is at length', () => {
    expect(spliceAt('done', 4, 0, ' deal')).toBe('done deal')
  })

  it('treats anchor 0 with no removal as a prepend', () => {
    expect(spliceAt('world', 0, 0, 'hello ')).toBe('hello world')
  })
})
