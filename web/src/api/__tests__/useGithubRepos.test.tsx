// Startup-health self-heal for the New Session repo dropdown (2026-08-29): a
// fetch that failed while the backend was still booting stranded the dropdown on
// "— could not load repos —" until a manual reload. The query now polls only
// while errored, so it recovers on its own once the edge/daemon is up.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useGithubRepos, reposRefetchInterval, REPOS_ERROR_REFETCH_MS } from '../hooks'
import { api } from '../client'

vi.mock('../client', () => ({ api: { get: vi.fn() }, ApiRequestError: class {} }))

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return createElement(QueryClientProvider, { client: qc }, children)
}

describe('reposRefetchInterval', () => {
  it('polls while errored so a boot-window failure self-heals', () => {
    expect(reposRefetchInterval({ state: { status: 'error' } })).toBe(REPOS_ERROR_REFETCH_MS)
  })

  it('does not poll a healthy or pending query', () => {
    expect(reposRefetchInterval({ state: { status: 'success' } })).toBe(false)
    expect(reposRefetchInterval({ state: { status: 'pending' } })).toBe(false)
  })
})

describe('useGithubRepos', () => {
  beforeEach(() => vi.clearAllMocks())

  it('recovers after an initial failure once the backend answers', async () => {
    // The hook retries once itself, so two rejections are needed to surface isError.
    ;(api.get as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce(new Error('daemon booting'))
      .mockRejectedValueOnce(new Error('daemon booting'))
      .mockResolvedValue([{ nameWithOwner: 'acme/widget', cloned: false }])

    const { result } = renderHook(() => useGithubRepos(), { wrapper })
    // The hook's own retry waits ~1s before the second attempt errors out.
    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 4000 })

    // The error-state poll re-runs the fetch; a manual refetch models the same path.
    await result.current.refetch()
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.[0].nameWithOwner).toBe('acme/widget')
  })
})
