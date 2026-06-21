// web/src/components/session/__tests__/useImageAttachments.test.tsx
import { renderHook, act, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useImageAttachments } from '../useImageAttachments'

beforeEach(() => {
  // jsdom lacks createObjectURL
  // @ts-expect-error test shim
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  // @ts-expect-error test shim
  URL.revokeObjectURL = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async () =>
    ({ ok: true, json: async () => ({ sha: 'abc', mime: 'image/png', name: 'x.png' }) })))
})

function pngFile(size = 10) {
  return new File([new Uint8Array(size)], 'x.png', { type: 'image/png' })
}

describe('useImageAttachments', () => {
  it('uploads a valid image and exposes a ref', async () => {
    const { result } = renderHook(() => useImageAttachments())
    await act(async () => { await result.current.addFiles([pngFile()]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    expect(result.current.refs()).toEqual([{ sha: 'abc', mime: 'image/png', name: 'x.png' }])
  })

  it('rejects an oversize file without uploading', async () => {
    const { result } = renderHook(() => useImageAttachments())
    const big = new File([new Uint8Array(6 * 1024 * 1024)], 'big.png', { type: 'image/png' })
    await act(async () => { await result.current.addFiles([big]) })
    expect(result.current.attachments[0].error).toMatch(/too large/i)
    expect(result.current.refs()).toHaveLength(0)
  })

  it('rejects an unsupported type', async () => {
    const { result } = renderHook(() => useImageAttachments())
    const pdf = new File([new Uint8Array(10)], 'x.pdf', { type: 'application/pdf' })
    await act(async () => { await result.current.addFiles([pdf]) })
    expect(result.current.attachments[0].error).toMatch(/unsupported/i)
  })

  it('remove drops the attachment', async () => {
    const { result } = renderHook(() => useImageAttachments())
    await act(async () => { await result.current.addFiles([pngFile()]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    act(() => result.current.remove('abc'))
    expect(result.current.attachments).toHaveLength(0)
  })
})
