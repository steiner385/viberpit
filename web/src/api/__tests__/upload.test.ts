import { vi, it, expect, beforeEach } from 'vitest'
import { uploadAttachment } from '../client'

beforeEach(() => {
  global.fetch = vi.fn(async () => ({
    ok: true, status: 201,
    json: async () => ({ id: 'a1', mediaType: 'image/png', filename: 'a.png', byteSize: 3, url: '/api/attachments/a1' }),
  })) as any
})

it('POSTs multipart form data and returns the descriptor', async () => {
  const file = new File([new Uint8Array([1, 2, 3])], 'a.png', { type: 'image/png' })
  const res = await uploadAttachment(file)
  expect(res.id).toBe('a1')
  const [url, init] = (global.fetch as any).mock.calls[0]
  expect(url).toBe('/api/attachments')
  expect(init.method).toBe('POST')
  expect(init.body).toBeInstanceOf(FormData)
})
