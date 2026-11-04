import { describe, it, expect } from 'vitest'
import { memoryTypeLabel } from './memoryFiles'

describe('memoryTypeLabel', () => {
  it('reads AutoMem as "Auto memory" and shows every other type as the CLI sent it', () => {
    expect(memoryTypeLabel('AutoMem')).toBe('Auto memory')
    expect(['User', 'Project', 'Local', 'Managed', 'Plugin'].map(memoryTypeLabel))
      .toEqual(['User', 'Project', 'Local', 'Managed', 'Plugin'])
  })
})
