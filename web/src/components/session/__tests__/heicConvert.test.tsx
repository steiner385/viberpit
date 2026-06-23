import { describe, it, expect, vi, beforeEach } from 'vitest'

const heic2any = vi.fn(async () => new Blob([new Uint8Array(4)], { type: 'image/jpeg' }))
vi.mock('heic2any', () => ({ default: heic2any }))

import { isHeic, maybeConvertHeic } from '../heicConvert'

beforeEach(() => { heic2any.mockClear() })

function file(name: string, type: string) {
  return new File([new Uint8Array(8)], name, { type })
}

describe('isHeic', () => {
  it('detects by mime type', () => {
    expect(isHeic(file('x.bin', 'image/heic'))).toBe(true)
    expect(isHeic(file('x.bin', 'image/heif'))).toBe(true)
  })
  it('detects by extension when type is empty', () => {
    expect(isHeic(file('IMG_1.HEIC', ''))).toBe(true)
    expect(isHeic(file('clip.heif', ''))).toBe(true)
  })
  it('returns false for normal images', () => {
    expect(isHeic(file('x.png', 'image/png'))).toBe(false)
  })
})

describe('maybeConvertHeic', () => {
  it('converts a HEIC file to a jpeg File', async () => {
    const out = await maybeConvertHeic(file('photo.heic', 'image/heic'))
    expect(heic2any).toHaveBeenCalledOnce()
    expect(out.type).toBe('image/jpeg')
    expect(out.name).toBe('photo.jpg')
  })
  it('converts when detected by extension with empty type', async () => {
    const out = await maybeConvertHeic(file('IMG_1.HEIC', ''))
    expect(out.name).toBe('IMG_1.jpg')
    expect(out.type).toBe('image/jpeg')
  })
  it('passes a non-HEIC file through unchanged without calling heic2any', async () => {
    const png = file('x.png', 'image/png')
    const out = await maybeConvertHeic(png)
    expect(out).toBe(png)
    expect(heic2any).not.toHaveBeenCalled()
  })
  it('handles heic2any returning a Blob array (multi-image HEIC)', async () => {
    heic2any.mockResolvedValueOnce([new Blob([new Uint8Array(4)], { type: 'image/jpeg' })] as unknown as Blob)
    const out = await maybeConvertHeic(file('live.heic', 'image/heic'))
    expect(out.type).toBe('image/jpeg')
  })
  it('rejects when conversion throws', async () => {
    heic2any.mockRejectedValueOnce(new Error('decode failed'))
    await expect(maybeConvertHeic(file('bad.heic', 'image/heic'))).rejects.toThrow()
  })
})
