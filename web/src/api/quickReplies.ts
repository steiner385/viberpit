import { useQuery } from '@tanstack/react-query'
import { api } from './client'

/** Personal replies span sessions and agent accounts, but never authenticated users. */
export function useQuickReplies() {
  const identity = useQuery({
    queryKey: ['auth', 'me'],
    queryFn: () => api.get<{ identity: string }>('/auth/me'),
    staleTime: 0,
    gcTime: 0,
    refetchInterval: 60_000,
    retry: false,
  })
  const query = useQuery({
    queryKey: ['quick-replies', identity.data?.identity],
    queryFn: () => api.get<{ replies: string[] }>('/quick-replies'),
    enabled: !!identity.data?.identity && !identity.isError,
    staleTime: 60_000,
    gcTime: 0,
    refetchInterval: 60_000,
    retry: false,
  })
  const replies = identity.isError ? [] : query.data?.replies ?? []
  const statusText = replies.length ? null
    : identity.isError || query.isError ? 'Quick replies unavailable. Try again later.'
      : identity.isPending || query.isPending ? 'Loading your replies…'
        : 'Learning from your replies across sessions.'
  return { replies, statusText }
}
