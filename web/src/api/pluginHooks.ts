// Settings › Plugins (D-16c): installed plugins, marketplaces, browse and the fanned-out
// mutations. Its own module so it ships in the lazy section chunk, not the entry bundle.
import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import { QK } from './hooks'
import type {
  LibraryPluginDetail, LibraryPluginsResponse, PluginBrowseQuery, PluginBrowseResponse, PluginOpResult,
} from './types'

export function useLibraryPlugins() {
  return useQuery<LibraryPluginsResponse>({
    queryKey: QK.libraryPlugins,
    queryFn: () => api.get<LibraryPluginsResponse>('/library/plugins'),
    staleTime: 30_000,
  })
}

export function useLibraryPluginDetail(id: string, enabled: boolean) {
  return useQuery<LibraryPluginDetail>({
    queryKey: QK.libraryPlugin(id),
    queryFn: () => api.get<LibraryPluginDetail>(`/library/plugins/${encodeURIComponent(id)}`),
    enabled,
    staleTime: 60_000,
  })
}

/** Everything a plugin or marketplace change can move: installed rows, browse, the
 *  detail cache, and the skills/commands/servers plugins contribute. */
function invalidatePluginViews(qc: QueryClient) {
  void qc.invalidateQueries({ queryKey: QK.libraryPlugins })
  void qc.invalidateQueries({ queryKey: QK.libraryPluginAll })
  void qc.invalidateQueries({ queryKey: QK.libraryPluginBrowseAll })
  void qc.invalidateQueries({ queryKey: QK.librarySkills })
  void qc.invalidateQueries({ queryKey: QK.libraryCommands })
  void qc.invalidateQueries({ queryKey: QK.libraryMcp })
}

/**
 * A fanned-out plugin mutation (D-16c, D2). The section renders the per-host outcome
 * and any refusal inline, so the global error toast is suppressed; every outcome
 * (even a partial failure, which is a 200) refetches what it could have changed.
 */
function usePluginOp<V, R = PluginOpResult>(mutationFn: (vars: V) => Promise<R>) {
  const qc = useQueryClient()
  return useMutation<R, ApiRequestError, V>({
    mutationFn,
    meta: { suppressErrorToast: true },
    onSettled: () => invalidatePluginViews(qc),
  })
}

/** Enable/disable on every host through the CLI wrapper (the single writer). */
export function useTogglePlugin() {
  return usePluginOp<{ id: string; enabled: boolean }, PluginOpResult & { id: string; enabled: boolean }>(
    ({ id, enabled }) => api.patch(`/library/plugins/${encodeURIComponent(id)}`, { enabled }),
  )
}

/** Installable plugins, searched and paged server-side (confirmed marketplaces plus
 *  reviewed Directory listings only). */
export function usePluginBrowse(query: PluginBrowseQuery, enabled = true) {
  return useQuery<PluginBrowseResponse>({
    queryKey: QK.libraryPluginBrowse(query),
    queryFn: () => {
      const params = new URLSearchParams({ offset: String(query.offset), limit: String(query.limit) })
      if (query.q) params.set('q', query.q)
      if (query.marketplace) params.set('marketplace', query.marketplace)
      return api.get<PluginBrowseResponse>(`/library/plugins/browse?${params.toString()}`)
    },
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  })
}

/** Install after the consent card: the server refuses without `confirm: true`. */
export function useInstallPlugin() {
  return usePluginOp<string>((id) =>
    api.post(`/library/plugins/${encodeURIComponent(id)}/install`, { confirm: true }))
}

/** Update after the consent card: an update pulls new code, so it is gated like install. */
export function useUpdatePlugin() {
  return usePluginOp<string>((id) =>
    api.post(`/library/plugins/${encodeURIComponent(id)}/update`, { confirm: true }))
}

/** Uninstall on every host; the server always keeps the plugin's data. */
export function useUninstallPlugin() {
  return usePluginOp<string>((id) => api.del(`/library/plugins/${encodeURIComponent(id)}`))
}

/** Add a marketplace at the exact source the owner reviewed. */
export function useAddMarketplace() {
  return usePluginOp<string, PluginOpResult & { marketplaces: string[] }>((source) =>
    api.post('/library/marketplaces', { source, confirm: true }))
}

/** Confirm (or re-confirm) a marketplace at the `normalizedSource` the page showed. */
export function useConfirmMarketplace() {
  return usePluginOp<{ name: string; source: string }, { name: string; confirmedSource: string; curation: string }>(
    ({ name, source }) =>
      api.post(`/library/marketplaces/${encodeURIComponent(name)}/confirm`, { source, confirm: true }),
  )
}

export function useUpdateMarketplace() {
  return usePluginOp<string>((name) =>
    api.post(`/library/marketplaces/${encodeURIComponent(name)}/update`, {}))
}

export function useRemoveMarketplace() {
  return usePluginOp<string>((name) => api.del(`/library/marketplaces/${encodeURIComponent(name)}`))
}
