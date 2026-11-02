// The session composer's `@` menu (D-7): `MentionMenu` with a Files group fed by the
// session's worktree file list. Mounted only while the menu is open, so the query
// (and its debounce) lives exactly as long as one mention attempt — the first fetch
// is the bare `@` (most recently modified files), then one per settled query.
import { forwardRef, useMemo } from 'react'
import { useSessionFiles } from '@/api/hooks'
import { useDebouncedValue } from '@/lib/useDebouncedValue'
import { FILE_SEARCH_DEBOUNCE_MS } from '../files/types'
import { MentionMenu, type MentionMenuHandle } from './MentionMenu'

type Props = Omit<React.ComponentProps<typeof MentionMenu>, 'files' | 'filesLoading' | 'ref'> & {
  sessionId: string
}

export const FileMentionMenu = forwardRef<MentionMenuHandle, Props>(function FileMentionMenu(
  { sessionId, query, ...rest }, ref,
) {
  const q = useDebouncedValue(query, FILE_SEARCH_DEBOUNCE_MS)
  // Previous results stay up while typing (the hook keeps placeholder data per session).
  const { data, isFetching } = useSessionFiles(sessionId, q)
  // A failed lookup degrades to an empty Files group (hidden), never a broken menu.
  // Memoised: a new array each render would reset the menu's highlight every render.
  const files = useMemo(() => data?.files.map((f) => f.path) ?? [], [data])
  return <MentionMenu ref={ref} {...rest} query={query} files={files} filesLoading={!data && isFetching} />
})
