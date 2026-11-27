import { describe, expect, it } from 'vitest'
import { base64urlToBytes, bytesToBase64url } from './base64url'

describe('base64url', () => {
  it('round-trips arbitrary bytes, including every value that differs from base64', () => {
    const bytes = new Uint8Array([0, 1, 2, 250, 251, 252, 253, 254, 255, 62, 63])
    const encoded = bytesToBase64url(bytes)
    expect(encoded).not.toMatch(/[+/=]/)
    expect(Array.from(base64urlToBytes(encoded))).toEqual(Array.from(bytes))
  })

  it('encodes an ArrayBuffer the way WebAuthn hands one back', () => {
    expect(bytesToBase64url(new Uint8Array([0xfb, 0xff]).buffer)).toBe('-_8')
  })

  it('decodes unpadded input (the VAPID key and WebAuthn challenge shape)', () => {
    expect(Array.from(base64urlToBytes('-_8'))).toEqual([0xfb, 0xff])
    expect(base64urlToBytes('').byteLength).toBe(0)
  })

  it('decodes into a standalone ArrayBuffer usable as a BufferSource', () => {
    const out = base64urlToBytes('AQID')
    expect(out.buffer.byteLength).toBe(3)
  })
})
