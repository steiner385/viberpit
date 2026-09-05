// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { isOutdated, isSafeToReload } from './version'

describe('isOutdated', () => {
  it('false when shas match', () => {
    expect(isOutdated('abc1234', 'abc1234')).toBe(false)
  })
  it('true when shas differ', () => {
    expect(isOutdated('abc1234', 'def5678')).toBe(true)
  })
  it('false when server sha is missing (cannot tell)', () => {
    expect(isOutdated('abc1234', null)).toBe(false)
    expect(isOutdated('abc1234', undefined)).toBe(false)
    expect(isOutdated('abc1234', '')).toBe(false)
  })
  it('false when build sha is the dev placeholder', () => {
    expect(isOutdated('dev', 'abc1234')).toBe(false)
  })
})

describe('isSafeToReload', () => {
  it('unsafe when prompts are buffered', () => {
    expect(isSafeToReload('idle', 1)).toBe(false)
  })
  it('unsafe when the focused session is mid-turn', () => {
    expect(isSafeToReload('in_progress', 0)).toBe(false)
    expect(isSafeToReload('waiting_input', 0)).toBe(false)
    expect(isSafeToReload('waiting_question', 0)).toBe(false)
  })
  it('safe when idle / no focused session / terminal statuses, nothing buffered', () => {
    expect(isSafeToReload('idle', 0)).toBe(true)
    expect(isSafeToReload(undefined, 0)).toBe(true)
    expect(isSafeToReload('done', 0)).toBe(true)
    expect(isSafeToReload('ready_review', 0)).toBe(true)
  })
})
