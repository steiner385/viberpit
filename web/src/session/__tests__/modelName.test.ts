// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { modelName, modelTier, isGatedTier } from '../modelName'

describe('modelName', () => {
  it('maps known claude ids to friendly names', () => {
    expect(modelName('claude-opus-4-8')).toBe('Opus 4.8')
    expect(modelName('claude-haiku-4-5')).toBe('Haiku 4.5')
    expect(modelName('claude-sonnet-4-6')).toBe('Sonnet 4.6')
  })
  it('handles the Claude 5 family ids, which carry no minor version', () => {
    expect(modelName('claude-fable-5')).toBe('Fable 5')
    expect(modelName('claude-opus-5')).toBe('Opus 5')
    // A dated id still formats from its version prefix.
    expect(modelName('claude-fable-5-20260101')).toBe('Fable 5')
    expect(modelName('claude-haiku-4-5-20251001')).toBe('Haiku 4.5')
  })
  it('returns unknown ids verbatim', () => {
    expect(modelName('gpt-4o')).toBe('gpt-4o')
  })
  it('returns empty string for null/empty', () => {
    expect(modelName(null)).toBe('')
    expect(modelName('')).toBe('')
  })
})

describe('modelTier', () => {
  it('returns the tier for full claude model ids', () => {
    expect(modelTier('claude-fable-5')).toBe('fable')
    expect(modelTier('claude-opus-4-8')).toBe('opus')
    expect(modelTier('claude-sonnet-4-6')).toBe('sonnet')
    expect(modelTier('claude-haiku-4-5')).toBe('haiku')
  })

  it('handles context-size suffixes like [1m]', () => {
    expect(modelTier('claude-fable-5[1m]')).toBe('fable')
    expect(modelTier('fable[1m]')).toBe('fable')
  })

  it('handles bare short aliases', () => {
    expect(modelTier('fable')).toBe('fable')
    expect(modelTier('opus')).toBe('opus')
    expect(modelTier('sonnet')).toBe('sonnet')
    expect(modelTier('haiku')).toBe('haiku')
  })

  it('maps opusplan to opus', () => {
    expect(modelTier('opusplan')).toBe('opus')
  })

  it('returns null for default, unknown, and null/undefined', () => {
    expect(modelTier('default')).toBeNull()
    expect(modelTier('gpt-4o')).toBeNull()
    expect(modelTier(null)).toBeNull()
    expect(modelTier(undefined)).toBeNull()
    expect(modelTier('')).toBeNull()
  })
})

describe('isGatedTier', () => {
  it('returns true only for fable tier models', () => {
    expect(isGatedTier('fable')).toBe(true)
    expect(isGatedTier('claude-fable-5')).toBe(true)
    expect(isGatedTier('claude-fable-5[1m]')).toBe(true)
  })

  it('returns false for non-fable tiers', () => {
    expect(isGatedTier('opus')).toBe(false)
    expect(isGatedTier('claude-opus-4-8')).toBe(false)
    expect(isGatedTier('opusplan')).toBe(false)
    expect(isGatedTier('default')).toBe(false)
    expect(isGatedTier(null)).toBe(false)
    expect(isGatedTier(undefined)).toBe(false)
  })
})
