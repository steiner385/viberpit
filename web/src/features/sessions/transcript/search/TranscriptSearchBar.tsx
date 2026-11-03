import { useEffect, useRef } from 'react'
import { ChevronDown, ChevronUp, X } from 'lucide-react'
import type { SearchHit, TranscriptSearchResult } from '@/api/transcript'
import { useTranscriptSearch } from './useTranscriptSearch'

interface Props {
  /** Run the search — `api/transcript`'s `fetchTranscriptSearch`, bound to the
   *  session by the transcript. Passed in rather than imported so this lazy chunk
   *  shares no module with the eager ones (each shared module is a new preloaded
   *  chunk). Stable across renders: a new function re-runs the search. */
  search: (q: string) => Promise<TranscriptSearchResult>
  /** Bumped by every ⌘F: (re)focus the input and select the query. */
  focusSignal: number
  /** Esc / ✕. The transcript restores focus. */
  onClose: () => void
  /** Scroll the transcript to `hit` — paging older history in first if needed. */
  onJump: (hit: SearchHit) => void
  /** The query the results on screen answer — the transcript highlights its
   *  terms — or '' for none. */
  onQuery: (query: string) => void
}

const iconButton = 'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-sm text-ink-muted'
  + ' hover:bg-surface-raised hover:text-ink disabled:opacity-40 disabled:hover:bg-transparent'
  + ' focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent'

/**
 * The ⌘F transcript search bar (D-11), at the top of the transcript pane. Loaded
 * lazily by `TranscriptView` — it is only ever needed once someone searches.
 *
 * Enter / Shift+Enter (and ↓ / ↑) step through the hits, wrapping; Esc and ✕
 * close it. The search itself is debounced and stale-proof (`useTranscriptSearch`).
 */
export default function TranscriptSearchBar({ search, focusSignal, onClose, onJump, onQuery }: Props) {
  const { query, setQuery, results, answered, step } = useTranscriptSearch(search)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [focusSignal])

  useEffect(() => { onQuery(answered) }, [answered, onQuery])

  const go = (dir: 'next' | 'prev') => {
    const hit = step(dir)
    if (hit) onJump(hit)
  }

  const count = results?.hits.length ?? 0
  const status = !results
      ? ''
      : count === 0
        ? 'No matches'
        : `${results.index === null ? 0 : results.index + 1} of ${count}${results.truncated ? '+' : ''}`

  return (
    <div
      role="search"
      aria-label="Transcript search"
      className="flex shrink-0 items-center gap-1 border-b border-edge bg-canvas px-2 py-1.5"
    >
      <input
        ref={inputRef}
        type="search"
        aria-label="Search this transcript"
        placeholder="Find in conversation"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            go(e.shiftKey ? 'prev' : 'next')
          } else if (e.key === 'Escape') {
            // Consumed: the shell's Esc rows (stop a turn, detach) skip a prevented event.
            e.preventDefault()
            e.stopPropagation()
            onClose()
          }
        }}
        className="min-w-0 flex-1 rounded-md border border-edge bg-surface px-2 py-1 text-sm text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      />
      <span aria-live="polite" className="min-w-[4.5rem] shrink-0 text-right text-xs tabular-nums text-ink-muted">
        {status}
      </span>
      <button type="button" aria-label="Previous match" title="Previous match (Shift+Enter)"
              className={iconButton} disabled={count === 0} onClick={() => go('prev')}>
        <ChevronUp size={16} aria-hidden="true" />
      </button>
      <button type="button" aria-label="Next match" title="Next match (Enter)"
              className={iconButton} disabled={count === 0} onClick={() => go('next')}>
        <ChevronDown size={16} aria-hidden="true" />
      </button>
      <button type="button" aria-label="Close search" title="Close (Esc)" className={iconButton} onClick={onClose}>
        <X size={16} aria-hidden="true" />
      </button>
    </div>
  )
}
