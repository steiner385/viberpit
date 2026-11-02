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
