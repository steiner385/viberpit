// web/src/api/libraryHooks.test.tsx
import { describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('./client', () => ({
  ApiRequestError: class extends Error {},
  api: {
    get: vi.fn().mockImplementation((path: string) => {
      if (path === '/library/plugins') {
        return Promise.resolve({ plugins: [{ id: 'a@m', name: 'a', marketplace: 'm', enabled: true, components: { commands: 0, skills: 0, agents: 0, mcpServers: 0, hooks: 0 } }], marketplaces: [] })
      }
      return Promise.resolve({})
    }),
    patch: vi.fn().mockResolvedValue({ id: 'a@m', enabled: false }),
    put: vi.fn(), del: vi.fn(), post: vi.fn(),
  },
}))

import { useAddMarketplace, useLibraryPlugins, usePluginBrowse, useTogglePlugin } from './pluginHooks'
import { api } from './client'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('library hooks', () => {
  it('fetches plugins', async () => {
    const { result } = renderHook(() => useLibraryPlugins(), { wrapper })
    await waitFor(() => expect(result.current.data?.plugins[0].id).toBe('a@m'))
  })

  it('toggle PATCHes the plugin', async () => {
    const { result } = renderHook(() => useTogglePlugin(), { wrapper })
    result.current.mutate({ id: 'a@m', enabled: false })
    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith('/library/plugins/a%40m', { enabled: false }))
  })

  it('browse sends search, filter and page to the server', async () => {
    renderHook(() => usePluginBrowse({ q: 'lint me', marketplace: 'acme', offset: 20, limit: 20 }), { wrapper })
    await waitFor(() => expect(api.get).toHaveBeenCalledWith(
      '/library/plugins/browse?offset=20&limit=20&q=lint+me&marketplace=acme'))
  })

  it('marketplace add posts the reviewed source with confirm: true', async () => {
    vi.mocked(api.post).mockResolvedValue({ op: 'marketplace_add', ok: true, hosts: [], marketplaces: [] })
    const { result } = renderHook(() => useAddMarketplace(), { wrapper })
    result.current.mutate('acme/tools')
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/library/marketplaces', { source: 'acme/tools', confirm: true }))
  })
})
