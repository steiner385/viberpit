import { useEffect, useMemo, useState } from 'react'

/**
 * The one import expression that names the diff highlighter's chunk. Every
 * loader goes through here so a second spelling of the specifier cannot drift
 * and quietly pull highlight.js onto the critical path (same reasoning as
 * `transcript/markdownChunk.ts`).
 */
type Highlighter = typeof import('./diffHighlight')

let pending: Promise<Highlighter | null> | null = null
let loaded: Highlighter | null = null

/** The highlighter if it has already loaded, so a later diff paints highlighted on its first render. */
export function loadedDiffHighlight(): Highlighter | null {
  return loaded
}

/** Load the highlighter once. Resolves null if the chunk will not load (a deploy in progress); plain text stands in. */
export function loadDiffHighlight(): Promise<Highlighter | null> {
  pending ??= import('./diffHighlight').then((m) => (loaded = m), () => {
    pending = null // let a later diff retry
    return null
  })
  return pending
}

/**
 * The line highlighter for `path`, or null while it loads / for plain text.
 * Shared by `DiffRenderer` and the Files tab's viewer (D-7), so both colour a
 * file the same way and load highlight.js through the one lazy chunk.
 */
export function useHighlighter(path: string): ((text: string) => string) | null {
  const [mod, setMod] = useState(loadedDiffHighlight)
  useEffect(() => {
    if (!path || mod) return
    let live = true
    void loadDiffHighlight().then((m) => { if (live && m) setMod(m) })
    return () => { live = false }
  }, [path, mod])
  return useMemo(() => (path && mod ? mod.highlighterFor(path) : null), [path, mod])
}
