import { describe, it, expect } from 'vitest'
import { isSafeHttpUrl } from './safeUrl'

describe('isSafeHttpUrl', () => {
  it('accepts http(s) only', () => {
    expect(isSafeHttpUrl('https://github.com/a/b')).toBe(true)
    expect(isSafeHttpUrl('HTTP://example.com')).toBe(true)
    expect(isSafeHttpUrl('javascript:alert(1)')).toBe(false)
    expect(isSafeHttpUrl(' javascript:alert(1)')).toBe(false)
    expect(isSafeHttpUrl('data:text/html,x')).toBe(false)
    expect(isSafeHttpUrl('//evil.com')).toBe(false)
    expect(isSafeHttpUrl(null)).toBe(false)
    expect(isSafeHttpUrl(undefined)).toBe(false)
  })
})
