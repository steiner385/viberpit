import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { usePendingDeploy } from './hooks'
import * as deploy from './deploy'

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) =>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('usePendingDeploy', () => {
  beforeEach(() => vi.restoreAllMocks())
  it('returns pending state from the API', async () => {
    vi.spyOn(deploy, 'fetchDeployStatus').mockResolvedValue({
      pending: { behind: true, liveSha: 'a', targetSha: 'b',
        commits: [{ sha: '1', subject: 's', author: 't', iso: 'i' }], ci: 'green' },
      deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: 'b' },
    })
    const { result } = renderHook(() => usePendingDeploy(), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.data?.behind).toBe(true))
    expect(result.current.data?.commits[0].subject).toBe('s')
  })
})
