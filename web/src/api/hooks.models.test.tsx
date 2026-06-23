import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useModels } from './hooks'
import { api } from './client'

vi.mock('./client', () => ({ api: { get: vi.fn() }, ApiRequestError: class {} }))

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('useModels', () => {
  beforeEach(() => vi.clearAllMocks())
  it('fetches /models', async () => {
    ;(api.get as ReturnType<typeof vi.fn>).mockResolvedValue([{ id: 'opus', label: 'Opus' }])
    const { result } = renderHook(() => useModels(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/models')
    expect(result.current.data).toEqual([{ id: 'opus', label: 'Opus' }])
  })
})
