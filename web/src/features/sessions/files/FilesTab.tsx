import { useState } from 'react'
import { cn } from '@/lib/utils'
import { useSessionFiles } from '@/api/hooks'
import { ApiRequestError, apiErrorDetail } from '@/api/client'
import { useDebouncedValue } from '@/lib/useDebouncedValue'
import { ListSearchInput } from '@/ui/list/ListSearchInput'
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import type { FileTarget } from '../sessionView'
import FileViewer from './FileViewer'
import { TextAction } from './TextAction'
import { FILE_SEARCH_DEBOUNCE_MS, MAX_SCAN_ENTRIES } from './types'

/**
 * The session's Files tab (D-7): a read-only look at the worktree. A search box
 * over the server's fuzzy file list (most recently modified first when empty)
 * and the `FileViewer`. The open file and line are URL state
 * (`?file=&line=`, `sessionView.useFileTarget`), which is how a transcript path
 * opens it at a line; `SessionBody` owns the hook and passes it in, so this lazy
 * chunk does not pull the router into a chunk of its own.
 *
 * Loaded with `React.lazy` from `SessionBody`. With no file open the list
 * fills the pane. With one open, on a pane of 48rem or more (`@3xl`, a
 * container query on the tab's own width) the list is a column beside the
 * viewer; narrower, the file replaces the list and All files goes back to it.
 * CSS, not a measured width: nothing here needs the layout in React state.
 */
export default function FilesTab({ sessionId, target: { file, line }, onOpen }: {
  sessionId: string
  target: FileTarget
  /** `useFileTarget`'s setter: a path opens it at its top, null goes back to the list. */
  onOpen: (path: string | null) => void
}) {
  return (
    <div data-testid="files-tab" className="@container flex h-full min-h-0 min-w-0">
      <FileList
        sessionId={sessionId}
        open={file}
        onOpen={onOpen}
        // With a file open: an 18rem column beside it, or (narrow) gone; without one, the whole pane.
        className={file ? 'hidden shrink-0 border-r border-edge @3xl:flex @3xl:w-72' : 'min-w-0 flex-1'}
      />
      {file && (
        <section aria-label="File" className="flex min-h-0 min-w-0 flex-1 flex-col">
          <header className="flex shrink-0 items-center gap-1 border-b border-edge bg-canvas px-2 py-1">
            <span className="shrink-0 @3xl:hidden"><TextAction onClick={() => onOpen(null)}>All files</TextAction></span>
            <h2 className="min-w-0 break-all px-1 font-mono text-xs text-ink">{file}</h2>
          </header>
          <FileViewer sessionId={sessionId} path={file} line={line} />
        </section>
      )}
    </div>
  )
}

function FileList({ sessionId, open, onOpen, className }: {
  sessionId: string
  open: string | null
  onOpen: (path: string) => void
  className?: string
}) {
  const [query, setQuery] = useState('')
  const q = useDebouncedValue(query.trim(), FILE_SEARCH_DEBOUNCE_MS)
  const { data, error, isError, refetch } = useSessionFiles(sessionId, q)

  let body
  if (!data) {
    body = isError
      ? <ListError error={error} onRetry={() => void refetch()} />
      : <p className="px-3 py-2 text-sm text-ink-muted" aria-busy="true">Loading files…</p>
  } else if (data.files.length === 0) {
    body = (
      <p className="px-3 py-2 text-sm text-ink-muted">
        {q ? <>No files match &ldquo;{q}&rdquo;.</> : 'No files in this worktree.'}
      </p>
    )
  } else {
    body = (
      <ul aria-label="Files" className="min-h-0 flex-1 overflow-y-auto py-1">
        {data.files.map(({ path }) => {
          const slash = path.lastIndexOf('/')
          const current = path === open
          return (
            <li key={path}>
              <button
                type="button"
                aria-current={current || undefined}
                onClick={() => onOpen(path)}
                className={cn(
                  'flex w-full min-w-0 flex-col items-start px-3 py-1 text-left',
                  TOUCH_FLOOR, 'justify-center',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent',
                  current ? 'bg-accent-wash text-accent' : 'text-ink hover:bg-surface-raised',
                )}
              >
                <span className="max-w-full truncate font-mono text-xs">{path.slice(slash + 1)}</span>
                {slash > 0 && (
                  <span className="max-w-full truncate text-xs text-ink-muted" dir="rtl">
                    {/* rtl truncates from the left, keeping the nearest directories; the bdi keeps the path's own order. */}
                    <bdi>{path.slice(0, slash)}</bdi>
                  </span>
                )}
              </button>
            </li>
          )
        })}
      </ul>
    )
  }

  return (
    <div className={cn('flex min-h-0 flex-col', className)}>
      <div className="shrink-0 border-b border-edge p-2">
        <ListSearchInput value={query} onChange={setQuery} ariaLabel="Search files" placeholder="Search files…" />
      </div>
      {data?.truncated && (
        <p role="note" className="shrink-0 px-3 pt-2 text-xs text-ink-muted">
          This worktree is large: only the first {MAX_SCAN_ENTRIES.toLocaleString('en-US')} files are searched.
        </p>
      )}
      {body}
    </div>
  )
}

function ListError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (error instanceof ApiRequestError && error.status === 409) {
    return (
      <p className="px-3 py-2 text-sm text-ink-muted">
        {apiErrorDetail(error).message ?? 'The session daemon cannot list files yet; try again after the next rollout.'}
      </p>
    )
  }
  return (
    <p className="px-3 py-2 text-sm text-ink-muted">
      Couldn&apos;t list the files.{' '}
      <TextAction onClick={onRetry}>Retry</TextAction>
    </p>
  )
}
