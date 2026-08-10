// web/src/components/session/__tests__/useImageAttachments.test.tsx
import { renderHook, act, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useImageAttachments } from '../useImageAttachments'

const heic2any = vi.fn(async () => new Blob([new Uint8Array(10)], { type: 'image/jpeg' }))
vi.mock('heic2any', () => ({ default: heic2any }))

beforeEach(() => {
  heic2any.mockClear()
  // jsdom lacks createObjectURL
  // @ts-expect-error test shim
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  // @ts-expect-error test shim
  URL.revokeObjectURL = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async () =>
    ({ ok: true, json: async () => ({ id: 'att-1', mediaType: 'image/png', filename: 'x.png', byteSize: 10, url: '/api/attachments/att-1' }) })))
})

function pngFile(size = 10) {
  return new File([new Uint8Array(size)], 'x.png', { type: 'image/png' })
}

describe('useImageAttachments', () => {
  it('uploads a valid image and exposes its attachment id', async () => {
    const { result } = renderHook(() => useImageAttachments())
    await act(async () => { await result.current.addFiles([pngFile()]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    expect(result.current.attachments[0].id).toBe('att-1')
    expect(result.current.refs()).toEqual(['att-1'])
  })

  it('rejects an oversize file without uploading', async () => {
    const { result } = renderHook(() => useImageAttachments())
    const big = new File([new Uint8Array(11 * 1024 * 1024)], 'big.png', { type: 'image/png' })
    await act(async () => { await result.current.addFiles([big]) })
    expect(result.current.attachments[0].error).toMatch(/too large/i)
    expect(result.current.refs()).toHaveLength(0)
  })

  it('rejects an unsupported type', async () => {
    const { result } = renderHook(() => useImageAttachments())
    const video = new File([new Uint8Array(10)], 'x.mp4', { type: 'video/mp4' })
    await act(async () => { await result.current.addFiles([video]) })
    expect(result.current.attachments[0].error).toMatch(/unsupported/i)
    // error chips render no thumbnail, so they hold no object URL
    expect(result.current.attachments[0].previewUrl).toBe('')
  })

  it('remove drops the attachment by key', async () => {
    const { result } = renderHook(() => useImageAttachments())
    await act(async () => { await result.current.addFiles([pngFile()]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    const key = result.current.attachments[0].key
    act(() => result.current.remove(key))
    expect(result.current.attachments).toHaveLength(0)
  })

  it('clear() empties attachments', async () => {
    const { result } = renderHook(() => useImageAttachments())
    await act(async () => { await result.current.addFiles([pngFile()]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    act(() => result.current.clear())
    expect(result.current.attachments).toHaveLength(0)
  })

  it('upload failure creates error chip', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      ({ ok: false, json: async () => ({}) })))
    const { result } = renderHook(() => useImageAttachments())
    await act(async () => { await result.current.addFiles([pngFile()]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    expect(result.current.attachments[0].error).toMatch(/upload failed/i)
    expect(result.current.refs()).toHaveLength(0)
  })

  it('caps multi-file batch at MAX_IMAGES (5)', async () => {
    const { result } = renderHook(() => useImageAttachments())
    const files = Array.from({ length: 7 }, (_, i) =>
      new File([new Uint8Array(10)], `img${i}.png`, { type: 'image/png' }))
    await act(async () => { await result.current.addFiles(files) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(5))
    expect(result.current.refs()).toHaveLength(5)
  })

  it('seeds from initial attachments (rehydrated draft)', () => {
    const initial = [{
      key: 'seed-0', id: 'att-9', mime: 'image/png',
      name: 's.png', previewUrl: '/api/attachments/att-9',
    }]
    const { result } = renderHook(() => useImageAttachments(initial))
    expect(result.current.attachments).toHaveLength(1)
    expect(result.current.attachments[0].previewUrl).toBe('/api/attachments/att-9')
    expect(result.current.refs()).toEqual(['att-9'])
  })

  it('converts a HEIC capture to JPEG and uploads it', async () => {
    const { result } = renderHook(() => useImageAttachments())
    const heic = new File([new Uint8Array(10)], 'photo.heic', { type: 'image/heic' })
    await act(async () => { await result.current.addFiles([heic]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    expect(heic2any).toHaveBeenCalledOnce()
    expect(result.current.attachments[0].error).toBeUndefined()
    expect(result.current.refs()).toEqual(['att-1'])
  })

  it('shows an error chip when HEIC conversion fails', async () => {
    heic2any.mockRejectedValueOnce(new Error('decode failed'))
    const fetchSpy = vi.fn(async () =>
      ({ ok: true, json: async () => ({ id: 'att-1', mediaType: 'image/png', filename: 'x.png', byteSize: 10, url: '/x' }) }))
    vi.stubGlobal('fetch', fetchSpy)
    const { result } = renderHook(() => useImageAttachments())
    const heic = new File([new Uint8Array(10)], 'photo.heic', { type: 'image/heic' })
    await act(async () => { await result.current.addFiles([heic]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    expect(result.current.attachments[0].error).toMatch(/read photo/i)
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(result.current.refs()).toHaveLength(0)
  })
})
