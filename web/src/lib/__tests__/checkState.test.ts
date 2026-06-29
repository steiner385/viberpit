import { describe, it, expect } from 'vitest'
import { checkStateColor, nodeCircleClass } from '@/lib/checkState'

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
