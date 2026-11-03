import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { SearchHit, TranscriptSearchResult } from '@/api/transcript'
import { parseSearchTerms } from './highlightRanges'

/** Typing pause before a search request goes out. */
export const SEARCH_DEBOUNCE_MS = 250
/** Shorter queries never search: one character matches nearly every turn. */
export const SEARCH_MIN_CHARS = 2

interface Results {
  /** The trimmed query these results answer. */
  query: string
  /** In transcript order, oldest first; when `truncated`, older matches were cut. */
  hits: SearchHit[]
  truncated: boolean
  /** The selected hit, or null before the first Enter / ↑ / ↓. */
  index: number | null
  /** This answer's arrival number, and whether the newest hit was auto-selected on
   *  it — the bar jumps once per arrival (after reporting the terms). */
  arrival: number
  auto: boolean
}

/**
 * The ⌘F bar's state (D-11): the query, its debounced results, and which hit is
 * selected.
 *
 * Stale responses are discarded by sequence number: every query change takes a new
 * number, and a response is applied only while its number is still the latest — so
 * a slow answer to `wid` can never overwrite the answer to `widget`, and nothing
 * lands for a query since cut below `SEARCH_MIN_CHARS`.
 *
 * Newest first: the reader sits at the bottom. When results land, the newest hit is
 * selected (`auto`) for the bar to jump to — but only if it is already loaded (`loaded`);
 * one that would need older history paged in waits for an explicit Enter, so typing
 * never pages. Stepping goes OLDER (Enter, ↑) or NEWER (Shift+Enter, ↓), wrapping.
 */
export function useTranscriptSearch(opts: {
  search: (q: string) => Promise<TranscriptSearchResult>
  /** Is this hit's turn already loaded (no paging needed to show it)? */
  loaded: (hit: SearchHit) => boolean
  onError: () => void
}) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Results | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const seq = useRef(0)
  const arrivals = useRef(0)
  // The callbacks a response reaches, current as of the response — not the request.
  const live = useRef(opts)
  useEffect(() => { live.current = opts })
  const { search } = opts
  const trimmed = query.trim()
  const searchable = trimmed.length >= SEARCH_MIN_CHARS

  useEffect(() => {
    const mine = ++seq.current
    if (!searchable) return
    const timer = setTimeout(() => {
      search(trimmed).then(
        (res) => {
          if (mine !== seq.current) return
          const newest = res.hits.length - 1
          const auto = newest >= 0 && live.current.loaded(res.hits[newest])
          setFailed(null)
          setResults({
            query: trimmed, hits: res.hits, truncated: res.truncated,
            index: auto ? newest : null, arrival: ++arrivals.current, auto,
          })
        },
        () => {
          if (mine !== seq.current) return
          setResults(null)
          setFailed(trimmed)
          live.current.onError()
        },
      )
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [search, trimmed, searchable])

  // A short query shows nothing; a longer one keeps the previous answer on screen
  // until its own arrives.
  const shown = searchable ? results : null
  const error = searchable && failed === trimmed

  /** Select the next older / newer hit, wrapping; null when there is none. The
   *  first step from no selection goes to the newest hit either way. */
  const step = useCallback((dir: 'older' | 'newer'): number | null => {
    if (!shown || shown.hits.length === 0) return null
    const n = shown.hits.length
    const index = shown.index === null
      ? n - 1
      : (shown.index + (dir === 'newer' ? 1 : -1) + n) % n
    setResults({ ...shown, index })
    return index
  }, [shown])

  const answered = shown?.query ?? ''
  const terms = useMemo(() => parseSearchTerms(answered), [answered])
  return { query, setQuery, results: shown, terms, error, step }
}
