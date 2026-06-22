// web/src/components/session/__tests__/useAttachments.test.ts
import { renderHook, act, waitFor } from '@testing-library/react'
import { vi, it, expect, beforeEach } from 'vitest'
import { useAttachments } from '../useAttachments'

vi.mock('@/api/client', () => ({
  uploadAttachment: vi.fn(async (f: File) => ({
    id: 'srv-' + f.name, mediaType: f.type, filename: f.name, byteSize: 3,
    url: '/api/attachments/srv-' + f.name,
  })),
}))

beforeEach(() => { (globalThis as any).URL.createObjectURL = vi.fn(() => 'blob:x') })

function png(name: string) { return new File([new Uint8Array([1])], name, { type: 'image/png' }) }

it('uploads added files and exposes their ids', async () => {
  const { result } = renderHook(() => useAttachments())
  await act(async () => { await result.current.addFiles([png('a.png')]) })
  await waitFor(() => expect(result.current.ids).toEqual(['srv-a.png']))
})

it('rejects non-images and over-count', async () => {
  const { result } = renderHook(() => useAttachments())
  await act(async () => {
    await result.current.addFiles([new File([new Uint8Array([1])], 'x.pdf', { type: 'application/pdf' })])
  })
  expect(result.current.error).toBeTruthy()
  expect(result.current.ids).toEqual([])
})

it('rejects an oversize file and sets error without uploading', async () => {
  const { result } = renderHook(() => useAttachments())
  const big = new File([new Uint8Array([1])], 'big.png', { type: 'image/png' })
  Object.defineProperty(big, 'size', { value: 11 * 1024 * 1024 })
  await act(async () => { await result.current.addFiles([big]) })
  expect(result.current.error).toMatch(/larger than 10 MB/)
  expect(result.current.ids).toEqual([])
})

it('allows at most MAX_COUNT images and sets error when limit is hit', async () => {
  const { result } = renderHook(() => useAttachments())
  const files = Array.from({ length: 7 }, (_, i) => png(`img${i}.png`))
  await act(async () => { await result.current.addFiles(files) })
  await waitFor(() => expect(result.current.uploading).toBe(false))
  expect(result.current.ids.length).toBeLessThanOrEqual(5)
  expect(result.current.error).toMatch(/At most 5/)
})
