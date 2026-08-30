import { describe, it, expect } from 'vitest'
import { extractText, splitImages } from '../toolContent'

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

describe('splitImages', () => {
  const png = { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'abc123' } }

  it('passes string content through with no images', () => {
    expect(splitImages('hello')).toEqual({ images: [], rest: 'hello' })
  })

  it('extracts a base64 image block as a data URL and removes it from rest', () => {
    const { images, rest } = splitImages([{ type: 'text', text: 'a' }, png])
    expect(images).toEqual([{ url: 'data:image/png;base64,abc123', mime: 'image/png', name: '' }])
    expect(rest).toEqual([{ type: 'text', text: 'a' }])
  })

  it('defaults a missing media_type to image/png', () => {
    const { images } = splitImages([{ type: 'image', source: { type: 'base64', data: 'x' } }])
    expect(images[0].url).toBe('data:image/png;base64,x')
  })

  it('extracts a url-source image block', () => {
    const { images } = splitImages([{ type: 'image', source: { type: 'url', url: 'https://x/y.png' } }])
    expect(images).toEqual([{ url: 'https://x/y.png', mime: '', name: '' }])
  })

  it('leaves an elided image block (empty data) in rest so the [image] label survives', () => {
    const elided = { type: 'image', source: { type: 'base64', media_type: 'image/png', data: '' } }
    const { images, rest } = splitImages([elided])
    expect(images).toEqual([])
    expect(rest).toEqual([elided])
  })

  it('passes non-array content through untouched', () => {
    expect(splitImages({ results: [1] })).toEqual({ images: [], rest: { results: [1] } })
    expect(splitImages(null)).toEqual({ images: [], rest: null })
  })
})
