import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { QK } from '@/api/hooks'
import { useDebouncedValue } from '@/lib/useDebouncedValue'

export interface SessionSearch {
  /** Raw, un-debounced input value (bind to the search box). */
  query: string
  setQuery: (q: string) => void
  /** Matching session ids, or null when no filter is active (empty query / error). */
  matchedIds: Set<string> | null
  /** True when a non-empty query is actively filtering the list. */
  isFiltered: boolean
}

export function useSessionSearch(): SessionSearch {
  const [query, setQuery] = useState('')
  const debounced = useDebouncedValue(query, 250).trim()
  const enabled = debounced !== ''

  const { data, isError } = useQuery({
    queryKey: QK.sessionSearch(debounced),
    queryFn: () => api.get<{ ids: string[] }>(`/sessions/search?q=${encodeURIComponent(debounced)}`),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 2_000,
  })

  // Null filter when: empty query, first load (no data yet), or request error.
  // A failed/loading search must show the full list, never a misleading "no matches".
  const matchedIds = enabled && data && !isError ? new Set(data.ids) : null
  return { query, setQuery, matchedIds, isFiltered: matchedIds !== null }
}
