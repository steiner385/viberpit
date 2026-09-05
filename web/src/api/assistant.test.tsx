import { renderHook, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/client', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
import { api } from '@/api/client'
import { useAssistantSession, useAssistantStatus } from './assistant'

const wrap = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('assistant api', () => {
  it('reads status and creates a session', async () => {
    vi.mocked(api.get).mockResolvedValue({ enabled: true, sessionId: null, model: 'sonnet' })
    vi.mocked(api.post).mockResolvedValue({ id: 'a1', model: 'sonnet', state: 'created' })
    const wrapper = wrap()
    const { result } = renderHook(() => useAssistantStatus(), { wrapper })
    await waitFor(() => expect(result.current.data?.enabled).toBe(true))
    const m = renderHook(() => useAssistantSession(), { wrapper })
    const out = await m.result.current.mutateAsync({})
    expect(out.id).toBe('a1')
    expect(api.post).toHaveBeenCalledWith('/assistant/session', {})
  })
})
