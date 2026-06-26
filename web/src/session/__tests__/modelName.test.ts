import { describe, it, expect } from 'vitest'
import { modelName } from '../modelName'

describe('modelName', () => {
  it('maps known claude ids to friendly names', () => {
    expect(modelName('claude-opus-4-8')).toBe('Opus 4.8')
    expect(modelName('claude-haiku-4-5')).toBe('Haiku 4.5')
    expect(modelName('claude-sonnet-4-6')).toBe('Sonnet 4.6')
  })
  it('returns unknown ids verbatim', () => {
    expect(modelName('gpt-4o')).toBe('gpt-4o')
  })
  it('returns empty string for null/empty', () => {
    expect(modelName(null)).toBe('')
    expect(modelName('')).toBe('')
  })
})
