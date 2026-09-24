// Ported from plotroom@5166b52 frontend/src/shell/PipelineSwitcher.tsx (the hook
// half; the picker component was replaced by the page's `Tabs` subnav in
// finish-line row 5). Focus is sticky (last-focused, persisted) — there is no
// hardcoded "primary". The focused repo's name is the guardrail that any
// authoring action targets the intended pipeline, so it stays pinned and visible.
import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { storageKey, migrateStorageKey, migrateStorageNamespace } from '@/lib/storageKey'

const STORE_KEY = storageKey('pipeline.focused')
/** Pre-namespace key. Migrated forward on the first read of the store (row 7b);
 *  a load whose `?pipeline=` resolves never reads the store, so it migrates on
 *  the next plain load — harmless, a later `focus()` write to the new key wins. */
const LEGACY_STORE_KEY = 'workspace.focusedPipeline'
const URL_PARAM = 'pipeline'

/** Migrates the legacy key forward (once, idempotent) and reads the current value. */
function readStore(): string | null {
  try {
    migrateStorageNamespace(localStorage, STORE_KEY)
    migrateStorageKey(localStorage, LEGACY_STORE_KEY, STORE_KEY)
    return localStorage.getItem(STORE_KEY)
  } catch { return null }
}

/**
 * Sticky focused-pipeline state, read/written through react-router's
 * `useSearchParams` rather than a raw `history.replaceState` (row 5 review
 * residual #1555): a plain `history.replaceState` call bypasses the router's
 * own history listener, so `useLocation()` elsewhere on the page (e.g.
 * `shell/useAttractCycle`'s kiosk `search` capture, or the nav link matching
 * the current URL) kept reading the PREVIOUS search string until some other
 * navigation happened to resync it.
 *
 * Resolution order: `?pipeline=` URL param (#191) → localStorage → first repo.
 * The URL param is always READ, even when `persist` is false (kiosk) — a wall
 * display's own `?pipeline=org/repo` deep link must still seed its starting
 * repo (#1555; previously `useUrl=false` disabled the read too, so a kiosk
 * deep link silently fell back to `repos[0]`). Only the WRITE side — mirroring
 * a focus change back into localStorage and the URL — is gated on `persist`:
 * kiosk's repo auto-cycle (`useRepoCycle`) calls the same `focus()` every tick,
 * and must not stomp a desk session's sticky localStorage choice or rewrite
 * the display's own URL, which IS its configuration (#1555).
 */
export function useFocusedPipeline(repos: readonly string[], enabled = true, persist = true): [string | null, (repo: string) => void] {
  const [searchParams, setSearchParams] = useSearchParams()
  const readPipelineParam = useCallback(
    () => searchParams.get(URL_PARAM),
    [searchParams],
  )
  const [focused, setFocused] = useState<string | null>(() => {
    const p = readPipelineParam(); if (p && repos.includes(p)) return p
    const s = readStore(); if (s && repos.includes(s)) return s
    return repos[0] ?? null
  })
  // Adopt a focus once repos load (repos is empty on first render before the live
  // state streams in, so the initial useState lands on null) AND re-home if the
  // current focus disappears from the set. Bug: the old guard `if (focused && …)`
  // never adopted a repo when focus started null — Model/Optimize then stayed on
  // their "select a pipeline" empty state forever. (Found via live browser testing.)
  // #191: a ?pipeline= deep link wins over the sticky value here, because repos
  // load async — the initial useState above usually runs before they arrive.
  useEffect(() => {
    if (!enabled) return
    if (repos.length === 0) return
    if (!focused || !repos.includes(focused)) {
      const fromUrl = readPipelineParam()
      const stored = readStore()
      // eslint-disable-next-line react-hooks/set-state-in-effect -- async repo adoption: repos arrive after mount; setState is the intended outcome (verbatim from source)
      setFocused(
        fromUrl && repos.includes(fromUrl) ? fromUrl
          : stored && repos.includes(stored) ? stored
            : repos[0]!)
    }
  }, [repos, focused, enabled, readPipelineParam])
  const focus = useCallback((repo: string) => {
    setFocused(repo)
    if (!persist) return
    try { localStorage.setItem(STORE_KEY, repo) } catch { /* ignore */ }
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set(URL_PARAM, repo)
      return next
    }, { replace: true }) // focus is not a back-button step
  }, [persist, setSearchParams]) // setFocused is stable; storage is a global
  return [focused, focus]
}
