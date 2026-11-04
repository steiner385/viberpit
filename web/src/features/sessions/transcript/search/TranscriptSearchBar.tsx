import { useCallback, useEffect, useRef } from 'react'
import type { SearchHit, TranscriptSearchResult } from '@/api/transcript'
import { useTranscriptSearch } from './useTranscriptSearch'
import { jumpRequest, type JumpRequest } from './searchJump'

interface Props {
  /** Run the search — `api/transcript`'s `fetchTranscriptSearch`, bound to the
   *  session by the transcript. Stable across renders: a new function re-runs it. */
  search: (q: string) => Promise<TranscriptSearchResult>
  /** The transcript's `earliestTurn`: a hit at or after it is loaded, so arriving
   *  results may jump to it without paging. */
  earliestTurn: number
  /** Bumped by every ⌘F: (re)focus the input and select the query. */
  focusSignal: number
  /** Esc / ✕. The transcript restores focus. */
  onClose: () => void
  /** Scroll the transcript to a hit — paging older history in first if needed. */
  onJump: (req: JumpRequest) => void
  /** The terms to highlight: the answered query's, or none. */
  onTerms: (terms: readonly string[]) => void
  /** A failed search, for the transcript to toast. */
  onError: (message: string) => void
}

const iconButton = 'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-muted'
  + ' hover:bg-surface-raised hover:text-ink disabled:opacity-40 disabled:hover:bg-transparent'
  + ' focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent'

/** A 16px stroke icon in lucide's geometry, inline: this chunk imports nothing but
 *  React, so it shares no module with the eager chunks (each shared module would be
 *  split into a preloaded chunk of its own). */
function Icon({ d }: { d: string[] }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {d.map((p) => <path key={p} d={p} />)}
    </svg>
  )
}

/**
 * The ⌘F transcript search bar (D-11), at the top of the transcript pane. Loaded
 * lazily with the rest of the search kit — only ever needed once someone searches.
 *
 * Newest first: results land on the newest hit (when it is loaded). Enter / ↑ step
 * OLDER, Shift+Enter / ↓ NEWER, wrapping; the count stays in transcript order, so
 * the newest reads "M of M". Esc and ✕ close it.
 */
export default function TranscriptSearchBar({ search, earliestTurn, focusSignal, onClose, onJump, onTerms, onError }: Props) {
  const loaded = useCallback((hit: SearchHit) => hit.ordinal >= earliestTurn, [earliestTurn])
  const { query, setQuery, results, terms, error, step } = useTranscriptSearch({
    search,
    loaded,
    onError: () => onError('Couldn’t search this conversation'),
  })
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [focusSignal])

  // Terms first, then the arrival's auto-jump — declared in this order so they run
  // in it: a new query's terms reset the transcript's current hit, and must not
  // land after (and wipe) the jump to its own newest hit.
  useEffect(() => { onTerms(terms) }, [terms, onTerms])
  const jumpedFor = useRef(0)
  useEffect(() => {
    if (!results?.auto || results.index === null || results.arrival === jumpedFor.current) return
    jumpedFor.current = results.arrival
    onJump(jumpRequest(results.hits, results.index, terms))
  }, [results, terms, onJump])

  const go = (dir: 'older' | 'newer') => {
    const index = step(dir)
    if (index !== null && results) onJump(jumpRequest(results.hits, index, terms))
  }

  const count = results?.hits.length ?? 0
  const status = error
    ? 'Search failed'
    : !results
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
            go(e.shiftKey ? 'newer' : 'older')
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
      <button type="button" aria-label="Older match" title="Older match (Enter)"
              className={iconButton} disabled={count === 0} onClick={() => go('older')}>
        <Icon d={['m18 15-6-6-6 6']} />
      </button>
      <button type="button" aria-label="Newer match" title="Newer match (Shift+Enter)"
              className={iconButton} disabled={count === 0} onClick={() => go('newer')}>
        <Icon d={['m6 9 6 6 6-6']} />
      </button>
      <button type="button" aria-label="Close search" title="Close (Esc)" className={iconButton} onClick={onClose}>
        <Icon d={['M18 6 6 18', 'm6 6 12 12']} />
      </button>
    </div>
  )
}
