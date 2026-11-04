import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useSearchParamState } from '@/lib/useSearchParamState'

/**
 * The session's views (`docs/PAGE-TABS.md` §2: a Workspace view toggle, stored
 * in `?view=` rather than `?tab=` because it is not the page's partition by
 * question). Spec 2026-11-01 §2 orders them Conversation | Changes | … | Browser.
 *
 * One owner for the list and the param, because two components drive it:
 * `SessionBody`'s tab strip and the header's `+N −M` chip, which opens Changes.
 * Which views are actually LISTED is `SessionBody`'s call (Changes needs a
 * non-empty diff, PR (D-15) a PR in the session payload, Files a session cwd, Browser the X display, Details a pane
 * under 64rem — wider, its cards are in the rail); a view that is not listed
 * falls back to the conversation. Details is last (D-6).
 */
export const SESSION_VIEWS = ['conversation', 'changes', 'pr', 'files', 'browser', 'details'] as const
export type SessionView = (typeof SESSION_VIEWS)[number]

export function useSessionView(): [SessionView, (v: SessionView) => void] {
  return useSearchParamState<SessionView>('view', SESSION_VIEWS, 'conversation')
}

/** The Files tab's open file (`?file=`, worktree-relative) and line (`?line=`, 1-based). */
export interface FileTarget {
  file: string | null
  line: number | null
}

/**
 * `?file=` / `?line=` — where the Files tab (D-7) is open, and the one way to
 * move it: `setFile(path, line?)` opens `path` (at `line`, else its top) with
 * `view=files` in a single replace, so a transcript path link and the tab's own
 * file list cannot drift on how a file is opened; `setFile(null)` closes it,
 * back to the tab's file list. A `line` that is not a positive integer reads as
 * none.
 */
export function useFileTarget(): [FileTarget, (file: string | null, line?: number | null) => void] {
  const [params, setParams] = useSearchParams()
  const file = params.get('file') || null
  const line = Number(params.get('line'))
  const setFile = useCallback((path: string | null, at?: number | null) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('file')
      next.delete('line')
      if (path) {
        next.set('view', 'files')
        next.set('file', path)
        if (at && at > 0) next.set('line', String(at))
      }
      return next
    }, { replace: true })
  }, [setParams])
  return [{ file, line: file && line > 0 && Number.isInteger(line) ? line : null }, setFile]
}
