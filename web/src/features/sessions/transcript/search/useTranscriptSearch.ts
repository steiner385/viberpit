import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import type { SearchHit, TranscriptSearchResult } from '@/api/transcript'

/** Typing pause before a search request goes out. */
export const SEARCH_DEBOUNCE_MS = 250
/** Shorter queries never search: one character matches nearly every turn. */
export const SEARCH_MIN_CHARS = 2

interface Results {
  /** The trimmed query these results answer. */
  query: string
  hits: SearchHit[]
  truncated: boolean
  /** The selected hit, or null before the first Enter / ↑ / ↓. */
  index: number | null
}

/**
 * The ⌘F bar's state (D-11): the query, its debounced results, and which hit is
 * selected.
 *
 * Stale responses are discarded by sequence number: every query change takes a new
 * number, and a response is applied only while its number is still the latest — so
 * a slow answer to `wid` can never overwrite the answer to `widget`, and nothing
 * lands for a query since cut below `SEARCH_MIN_CHARS`.
 */
export function useTranscriptSearch(search: (q: string) => Promise<TranscriptSearchResult>) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Results | null>(null)
  const seq = useRef(0)
  const trimmed = query.trim()
  const searchable = trimmed.length >= SEARCH_MIN_CHARS

  useEffect(() => {
    const mine = ++seq.current
    if (!searchable) return
    const timer = setTimeout(() => {
      search(trimmed).then(
        (res) => {
          if (mine !== seq.current) return
          setResults({ query: trimmed, hits: res.hits, truncated: res.truncated, index: null })
        },
        () => {
          if (mine !== seq.current) return
          // A failed request toasts, as the app's other requests do; the bar keeps
          // the last answer it had (or none) rather than a count for this query.
          setResults(null)
          toast.error('Couldn’t search this conversation')
        },
      )
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [search, trimmed, searchable])

  // A short query shows nothing; a longer one keeps the previous answer on screen
  // until its own arrives.
  const shown = searchable ? results : null

  /** Select the next / previous hit, wrapping; null when there is none. The first
   *  step goes to the first hit (next) or the last, most recent one (previous). */
  const step = useCallback((dir: 'next' | 'prev'): SearchHit | null => {
    if (!shown || shown.hits.length === 0) return null
    const n = shown.hits.length
    const index = shown.index === null
      ? (dir === 'next' ? 0 : n - 1)
      : (shown.index + (dir === 'next' ? 1 : -1) + n) % n
    setResults({ ...shown, index })
    return shown.hits[index]
  }, [shown])

  /** The query the results on screen answer — what to highlight — or ''. */
  const answered = shown?.query ?? ''
  return { query, setQuery, results: shown, answered, step }
}
