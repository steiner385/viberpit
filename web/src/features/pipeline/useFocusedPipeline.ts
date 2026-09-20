// Ported from plotroom@5166b52 frontend/src/shell/PipelineSwitcher.tsx (the hook
// half; the picker component was replaced by the page's `Tabs` subnav in
// finish-line row 5). Focus is sticky (last-focused, persisted) — there is no
// hardcoded "primary". The focused repo's name is the guardrail that any
// authoring action targets the intended pipeline, so it stays pinned and visible.
import { useCallback, useEffect, useState } from 'react'
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

/** #191: the focused pipeline is shareable via ?pipeline=org/repo. */
function readPipelineParam(): string | null {
  try { return new URLSearchParams(location.search).get(URL_PARAM) } catch { return null }
}
function writePipelineParam(repo: string): void {
  try {
    const u = new URL(location.href)
    u.searchParams.set(URL_PARAM, repo)
    history.replaceState(history.state, '', u) // focus is not a back-button step
  } catch { /* ignore */ }
}

/** Sticky focused-pipeline state. Resolution order: ?pipeline= URL param (#191,
 *  when `useUrl`) → localStorage → first repo. */
export function useFocusedPipeline(repos: readonly string[], enabled = true, useUrl = true): [string | null, (repo: string) => void] {
  const [focused, setFocused] = useState<string | null>(() => {
    if (useUrl) { const p = readPipelineParam(); if (p && repos.includes(p)) return p }
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
      const fromUrl = useUrl ? readPipelineParam() : null
      const stored = readStore()
      // eslint-disable-next-line react-hooks/set-state-in-effect -- async repo adoption: repos arrive after mount; setState is the intended outcome (verbatim from source)
      setFocused(
        fromUrl && repos.includes(fromUrl) ? fromUrl
          : stored && repos.includes(stored) ? stored
            : repos[0]!)
    }
  }, [repos, focused, enabled, useUrl])
  const focus = useCallback((repo: string) => {
    setFocused(repo)
    try { localStorage.setItem(STORE_KEY, repo) } catch { /* ignore */ }
    if (useUrl) writePipelineParam(repo)
  }, [useUrl]) // setFocused is stable; storage/URL are globals
  return [focused, focus]
}
