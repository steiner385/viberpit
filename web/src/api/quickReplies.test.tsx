import { describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useQuickReplies } from './quickReplies'
import { api } from './client'

vi.unmock('./quickReplies')
vi.mock('./client', () => ({ api: { get: vi.fn() } }))

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  return { client, ...renderHook(() => useQuickReplies(), { wrapper }) }
}

describe('personal quick replies', () => {
  it('loads the shared endpoint only after resolving the authenticated user', async () => {
    vi.mocked(api.get).mockImplementation(async (path) => path === '/auth/me' ? { identity: 'alice' } : { replies: ['Keep going'] })
    const { result } = setup()
    expect(result.current.statusText).toBe('Loading your replies…')
    await waitFor(() => expect(result.current.replies).toEqual(['Keep going']))
    expect(api.get).toHaveBeenCalledWith('/quick-replies')
    expect(result.current.statusText).toBeNull()
  })

  it('clears the displayed replies when the identity changes before the new response arrives', async () => {
    vi.mocked(api.get).mockImplementation(async (path) => path === '/auth/me' ? { identity: 'alice' } : { replies: ['Alice reply'] })
    const { client, result } = setup()
    await waitFor(() => expect(result.current.replies).toEqual(['Alice reply']))
    vi.mocked(api.get).mockImplementation(() => new Promise(() => {}))
    act(() => { client.setQueryData(['auth', 'me'], { identity: 'bob' }) })
    await waitFor(() => expect(result.current.replies).toEqual([]))
    expect(result.current.statusText).toBe('Loading your replies…')
  })

  it('shows a learning state for an empty corpus', async () => {
    vi.mocked(api.get).mockImplementation(async (path) => path === '/auth/me' ? { identity: 'alice' } : { replies: [] })
    const { result } = setup()
    await waitFor(() => expect(result.current.statusText).toBe('Learning from your replies across sessions.'))
  })

  it('shows unavailable without canned replies when the endpoint fails', async () => {
    vi.mocked(api.get).mockImplementation(async (path) => {
      if (path === '/auth/me') return { identity: 'alice' }
      throw new Error('unavailable')
    })
    const { result } = setup()
    await waitFor(() => expect(result.current.statusText).toBe('Quick replies unavailable. Try again later.'))
    expect(result.current.replies).toEqual([])
  })
})
