import { useMemo } from 'react'
import { useSessions } from '@/api/hooks'
import type { MentionSource } from './MentionMenu'

/** The sessions an `@` menu can mention (spec v2 §10.2), from the warm sessions list.
 *  `excludeId` drops the composer's own session — mentioning yourself is noise. */
export function useMentionSessions(excludeId?: string): MentionSource[] {
  const { data: sessions } = useSessions()
  return useMemo(
    () => (sessions ?? []).filter((s) => s.id !== excludeId).map((s) => ({ id: s.id, title: s.name })),
    [sessions, excludeId],
  )
}
