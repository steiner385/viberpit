import { describe, it, expect } from 'vitest'
import { checkStateColor, checkStateTexture, nodeCircleClass } from '@/lib/checkState'

describe('checkState palette', () => {
  it('maps done to green and failed to red', () => {
    expect(checkStateColor('done')).toContain('green')
    expect(checkStateColor('failed')).toContain('red')
    expect(checkStateColor('overdue')).toContain('amber')
  })
  it('nodeCircleClass keeps the active ring', () => {
    expect(nodeCircleClass('active')).toContain('ring-2')
  })
})

describe('checkStateColor (new states)', () => {
  it('maps skipped and queued to muted gray', () => {
    expect(checkStateColor('skipped')).toContain('gray')
    expect(checkStateColor('queued')).toContain('gray')
  })
})

describe('checkStateTexture', () => {
  it('gives overdue a 90deg white stripe overlay', () => {
    expect(checkStateTexture('overdue')).toMatch(/repeating-linear-gradient\(90deg/)
  })
  it('gives failed a -45deg white stripe overlay', () => {
    expect(checkStateTexture('failed')).toMatch(/repeating-linear-gradient\(-45deg/)
  })
  it('returns empty string for plain states', () => {
    expect(checkStateTexture('done')).toBe('')
    expect(checkStateTexture('active')).toBe('')
    expect(checkStateTexture('queued')).toBe('')
  })
})
