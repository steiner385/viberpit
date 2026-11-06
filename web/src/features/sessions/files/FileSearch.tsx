import { useEffect, useId, useMemo, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useSessionFileSearch } from '@/api/hooks'
import { ApiRequestError, apiErrorCode, apiErrorDetail } from '@/api/client'
import { cn } from '@/lib/utils'
import { useDebouncedValue } from '@/lib/useDebouncedValue'
import { ListSearchInput } from '@/ui/list/ListSearchInput'
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import { MARK_CLASS } from '../transcript/search/searchMarks'
import { TextAction } from './TextAction'
import { CONTENT_SEARCH_DEBOUNCE_MS, MIN_CONTENT_QUERY_CHARS, type SessionFileMatch } from './types'

/**
 * The Files tab's content search (D-20): `git grep` over the session's worktree
 * (`GET /sessions/{id}/files/search`), results grouped by file, and a result
 * opens the viewer at its line through `useFileTarget`'s setter.
 *
 * Lazy: `FilesTab` imports it only when the user switches its list to Contents.
 * The input keeps focus throughout (a combobox driving a listbox through
 * `aria-activedescendant`, as `AppPalette` does), so ↑/↓ move the active result
 * and Enter opens it — the first one until ↑/↓ move it. While the rows answer an
 * older query (the debounce or the fetch has not caught up) they show dimmed and
 * Enter waits, so it never opens a match the typed query does not have.
 *
 * Line text is rendered as React text nodes only; the match is a `<mark>` around
 * a text slice. Nothing from a file ever reaches the DOM as HTML.
 */
export default function FileSearch({ sessionId, initialQuery = '', onOpen, trailing }: {
  sessionId: string
  initialQuery?: string
  onOpen: (path: string, line: number) => void
  /** Rendered beside the box — `FilesTab`'s switch back to the file list. */
  trailing?: ReactNode
}) {
  const [query, setQuery] = useState(initialQuery)
  const [regex, setRegex] = useState(false)
  const debounced = useDebouncedValue(query, CONTENT_SEARCH_DEBOUNCE_MS)
  const q = debounced.length >= MIN_CONTENT_QUERY_CHARS ? debounced : ''
  const { data: raw, error, isError, isPlaceholderData, refetch } = useSessionFileSearch(sessionId, q, regex)
  // A disabled query still hands back its placeholder: below the minimum there is nothing to show.
  const data = q ? raw : undefined
  const stale = query !== debounced || isPlaceholderData

  const groups = useMemo(() => {
    const byPath = new Map<string, SessionFileMatch[]>()
    for (const m of data?.matches ?? []) {
      const rows = byPath.get(m.path)
      if (rows) rows.push(m)
      else byPath.set(m.path, [m])
    }
    return [...byPath]
  }, [data])
  const flat = useMemo(() => groups.flatMap(([, rows]) => rows), [groups])

  // The active row belongs to the result set it was picked in: new results start at the top.
  const [sel, setSel] = useState<{ of: typeof data; i: number }>({ of: undefined, i: 0 })
  const active = flat.length === 0 ? -1 : sel.of === data ? Math.min(sel.i, flat.length - 1) : 0
  const listId = useId()
  const optionId = (i: number) => `${listId}-${i}`
  const activeId = active >= 0 ? optionId(active) : undefined
  useEffect(() => {
    if (activeId) document.getElementById(activeId)?.scrollIntoView?.({ block: 'nearest' })
  }, [activeId])

  const open = (m: SessionFileMatch) => onOpen(m.path, m.line)
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (!stale && active >= 0) open(flat[active])
      return
    }
    if (flat.length === 0) return
    const step = e.key === 'ArrowDown' ? 1 : e.key === 'ArrowUp' ? -1 : 0
    if (step === 0) return
    e.preventDefault()
    setSel({ of: data, i: (active + step + flat.length) % flat.length })
  }

  let body: ReactNode
  if (!q) {
    body = <Note>Type at least {MIN_CONTENT_QUERY_CHARS} characters to search file contents.</Note>
  } else if (isError) {
    body = <SearchError error={error} onRetry={() => void refetch()} />
  } else if (!data) {
    body = <Note busy>Searching…</Note>
  } else if (flat.length === 0) {
    body = <Note>No matches</Note>
  } else {
    let i = -1
    body = (
      <div
        id={listId}
        role="listbox"
        aria-label="Search results"
        aria-busy={stale || undefined}
        className={cn('min-h-0 flex-1 overflow-y-auto py-1', stale && 'opacity-60')}
      >
        {groups.map(([path, rows], g) => {
          const headingId = `${listId}-h${g}`
          return (
            <div key={path} role="group" aria-labelledby={headingId}>
              <div id={headingId} className="truncate px-3 pt-2 pb-1 font-mono text-xs font-medium text-ink-muted" dir="rtl">
                <bdi>{path}</bdi>
              </div>
              {rows.map((m) => {
                const at = ++i
                return (
                  <div
                    key={`${m.line}:${at}`}
                    id={optionId(at)}
                    role="option"
                    aria-selected={at === active}
                    // Keep focus in the box: a click picks, it does not blur the combobox.
                    onMouseDown={(ev) => ev.preventDefault()}
                    onMouseMove={() => { if (at !== active) setSel({ of: data, i: at }) }}
                    onClick={() => open(m)}
                    className={cn(
                      'flex cursor-default items-center gap-2 px-3 py-1 text-left',
                      TOUCH_FLOOR,
                      at === active ? 'bg-accent-wash' : 'hover:bg-surface-raised',
                    )}
                  >
                    <span className="w-10 shrink-0 text-right font-mono text-2xs tabular-nums text-ink-faint">{m.line}</span>
                    <span className="min-w-0 flex-1 truncate whitespace-pre font-mono text-xs text-ink">
                      <MatchText {...m} />
                    </span>
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b border-edge p-2">
        <ListSearchInput
          value={query}
          onChange={setQuery}
          ariaLabel="Search file contents"
          placeholder="Search contents…"
          className="min-w-0 flex-1"
          inputProps={{
            role: 'combobox',
            autoFocus: true,
            autoComplete: 'off',
            spellCheck: false,
            'aria-expanded': flat.length > 0,
            'aria-controls': flat.length > 0 ? listId : undefined,
            'aria-autocomplete': 'list',
            'aria-activedescendant': activeId,
            onKeyDown,
          }}
        />
        <button
          type="button"
          aria-pressed={regex}
          onClick={() => setRegex((r) => !r)}
          title="Regular expression"
          className={cn(
            'inline-flex shrink-0 items-center justify-center rounded-md px-2 font-mono text-xs',
            TOUCH_FLOOR,
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            regex ? 'bg-accent-wash text-accent' : 'text-ink-muted hover:bg-surface-raised',
          )}
        >
          Regex
        </button>
        {trailing}
      </div>
      {data && !isError && data.timedOut && <Notice>Search timed out — showing partial results</Notice>}
      {data && !isError && data.truncated && !data.timedOut && <Notice>Showing first {data.matches.length} matches</Notice>}
      {body}
    </div>
  )
}

/** `text` with `[start, end)` marked. The offsets are the server's (Python) code-point
 *  indices, so the slicing is over code points too; an empty span marks nothing. */
function MatchText({ text, start, end }: SessionFileMatch) {
  if (!(end > start)) return <>{text}</>
  const cps = Array.from(text)
  return (
    <>
      {cps.slice(0, start).join('')}
      <mark className={MARK_CLASS}>{cps.slice(start, end).join('')}</mark>
      {cps.slice(end).join('')}
    </>
  )
}

function Note({ children, busy }: { children: ReactNode; busy?: boolean }) {
  return <p className="px-3 py-2 text-sm text-ink-muted" aria-busy={busy || undefined}>{children}</p>
}

function Notice({ children }: { children: ReactNode }) {
  return <p role="note" className="shrink-0 px-3 pt-2 text-xs text-ink-muted">{children}</p>
}

function SearchError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const code = apiErrorCode(error)
  const status = error instanceof ApiRequestError ? error.status : 0
  if (code === 'BAD_REGEX') return <Note>Invalid regular expression</Note>
  if (code === 'BAD_QUERY' || status === 422) return <Note>This search can&apos;t be run.</Note>
  if (status === 404) return <Note>This session has no worktree to search.</Note>
  if (status === 409) {
    return (
      <Note>
        {apiErrorDetail(error).message ?? 'The session daemon cannot search files yet; try again after the next rollout.'}
      </Note>
    )
  }
  return (
    <p className="px-3 py-2 text-sm text-ink-muted">
      Couldn&apos;t search the files.{' '}
      <TextAction onClick={onRetry}>Retry</TextAction>
    </p>
  )
}
