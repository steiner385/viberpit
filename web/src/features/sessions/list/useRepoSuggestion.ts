/**
 * Debounced "is this prompt for a different repository?" check for the
 * dispatch bar, backed by `POST /api/dispatch/repo-check` (`@/api/repoCheck`).
 *
 * - Fires `debounceMs` after the text goes idle once it has `minChars`.
 * - Results are cached by trimmed text (small insertion-ordered Map) so the
 *   same text never refires, and a response is only ever read under the text
 *   it was fired for — a stale answer can never show up under newer text.
 * - `ensureChecked()` is the submit-time gate: cached answer if there is one,
 *   else the in-flight request for this text, else a fresh request — waiting
 *   at most `submitWaitMs`. Fail-open everywhere: any error or timeout is
 *   "no suggestion". Never throws.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { checkRepo } from '@/api/repoCheck'

export interface RepoSuggestion {
  repo: string
  reason: string | null
}

export interface UseRepoSuggestionOptions {
  text: string
  /** The repository currently selected ('' = none). */
  repo: string
  /** false ⇒ never calls the server (and `ensureChecked` resolves null). */
  enabled: boolean
  debounceMs?: number
  minChars?: number
  submitWaitMs?: number
  /** Test seam; defaults to the real API call. */
  check?: typeof checkRepo
}

export interface UseRepoSuggestion {
  /** null when there is none, when it names the selected repo, or when it was dismissed for this text. */
  suggestion: RepoSuggestion | null
  /** Hide the suggestion for the current text; typing new text re-arms it. */
  dismiss: () => void
  /** Submit-time gate — see the module comment. */
  ensureChecked: () => Promise<RepoSuggestion | null>
}

/** Defaults tuned for a human typing; tests pass their own. */
export const REPO_CHECK_DEFAULTS = { debounceMs: 1500, minChars: 30, submitWaitMs: 3000 } as const

const CACHE_CAP = 20

type Cache = ReadonlyMap<string, RepoSuggestion | null>

interface Inflight {
  key: string
  promise: Promise<RepoSuggestion | null>
}

export function useRepoSuggestion({
  text,
  repo,
  enabled,
  debounceMs = REPO_CHECK_DEFAULTS.debounceMs,
  minChars = REPO_CHECK_DEFAULTS.minChars,
  submitWaitMs = REPO_CHECK_DEFAULTS.submitWaitMs,
  check = checkRepo,
}: UseRepoSuggestionOptions): UseRepoSuggestion {
  const key = text.trim()
  // The cache is state so render can read it; `cacheRef` is the same value for
  // the callbacks, written together with `setCache` so they never disagree.
  const [cache, setCache] = useState<Cache>(() => new Map())
  const cacheRef = useRef<Cache>(cache)
  const [dismissedKey, setDismissedKey] = useState<string | null>(null)
  const inflightRef = useRef<Inflight | null>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(false)
  // Latest render values for callbacks fired from timers / event handlers.
  const repoRef = useRef(repo)
  const checkRef = useRef(check)
  const keyRef = useRef(key)
  const dismissedKeyRef = useRef(dismissedKey)
  useLayoutEffect(() => {
    repoRef.current = repo
    checkRef.current = check
    keyRef.current = key
    dismissedKeyRef.current = dismissedKey
  }, [repo, check, key, dismissedKey])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
  }, [])

  /** Start (or join) the request for `k`. Never rejects. */
  const fire = useCallback((k: string): Promise<RepoSuggestion | null> => {
    const hit = cacheRef.current.get(k)
    if (hit !== undefined) return Promise.resolve(hit)
    if (inflightRef.current?.key === k) return inflightRef.current.promise
    // `entry` is compared by identity below: only the newest request holds the
    // in-flight slot, so a superseded one must not clear its successor.
    const entry = { key: k } as Inflight
    entry.promise = (async () => {
      let result: RepoSuggestion | null = null
      try {
        const r = await checkRef.current(k, repoRef.current || null)
        if (r?.suggested_repo) result = { repo: r.suggested_repo, reason: r.reason ?? null }
      } catch {
        result = null
      }
      const next = new Map(cacheRef.current)
      if (next.size >= CACHE_CAP) {
        const oldest = next.keys().next().value
        if (oldest !== undefined) next.delete(oldest)
      }
      next.set(k, result)
      cacheRef.current = next
      if (inflightRef.current === entry) inflightRef.current = null
      if (mountedRef.current) setCache(next)
      return result
    })()
    inflightRef.current = entry
    return entry.promise
  }, [])

  // Debounced background check while typing.
  useEffect(() => {
    if (!enabled || key.length < minChars) return
    if (cacheRef.current.has(key) || inflightRef.current?.key === key) return
    const timer = setTimeout(() => {
      debounceRef.current = null
      void fire(key)
    }, debounceMs)
    debounceRef.current = timer
    return () => {
      clearTimeout(timer)
      if (debounceRef.current === timer) debounceRef.current = null
    }
  }, [key, enabled, minChars, debounceMs, fire])

  const dismiss = useCallback(() => setDismissedKey(keyRef.current), [])

  const ensureChecked = useCallback(async (): Promise<RepoSuggestion | null> => {
    const k = keyRef.current
    if (!enabled || k.length < minChars) return null
    // Dismissed for this exact text ⇒ the user already answered "keep".
    if (dismissedKeyRef.current === k) return null
    const settle = (s: RepoSuggestion | null) => (s && s.repo !== repoRef.current ? s : null)
    const hit = cacheRef.current.get(k)
    if (hit !== undefined) return settle(hit)
    // Submitting IS the idle moment — do not let the debounce fire a second time.
    if (debounceRef.current) {
      clearTimeout(debounceRef.current)
      debounceRef.current = null
    }
    let timer: ReturnType<typeof setTimeout> | null = null
    const timeout = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), submitWaitMs)
    })
    try {
      return settle(await Promise.race([fire(k), timeout]))
    } catch {
      return null
    } finally {
      if (timer) clearTimeout(timer)
    }
  }, [enabled, minChars, submitWaitMs, fire])

  const cached = cache.get(key) ?? null
  const suggestion =
    cached && cached.repo !== repo && dismissedKey !== key ? cached : null

  return { suggestion, dismiss, ensureChecked }
}
