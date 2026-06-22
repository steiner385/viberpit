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
