import { describe, it, expect } from 'vitest'
import { extractText } from '../toolContent'

describe('extractText', () => {
  it('returns a plain string as-is', () => {
    expect(extractText('hello world')).toBe('hello world')
  })

  it('joins an array of text content blocks', () => {
    expect(extractText([{ type: 'text', text: 'a' }, { type: 'text', text: 'b' }])).toBe('a\nb')
  })

  it('labels non-text content blocks rather than serializing them', () => {
    expect(extractText([{ type: 'text', text: 'a' }, { type: 'image', source: {} }])).toBe('a\n[image]')
  })

  it('returns null for a plain object (structured data, not text)', () => {
    expect(extractText({ results: [1, 2, 3] })).toBeNull()
  })

  it('returns null for an array of non-content-block objects (structured)', () => {
    expect(extractText([{ pr: 77, state: 'MERGED' }])).toBeNull()
  })

  it('returns null for null/undefined', () => {
    expect(extractText(null)).toBeNull()
    expect(extractText(undefined)).toBeNull()
  })
})
