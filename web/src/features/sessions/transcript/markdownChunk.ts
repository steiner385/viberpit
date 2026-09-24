/**
 * The one import expression that names the markdown renderer's chunk (#1411).
 *
 * `Markdown.tsx` turns it into a `lazy()` boundary and `TranscriptView` warms it
 * on mount. Both go through this module rather than each writing their own
 * `import('./MarkdownDoc')`, because a bundler splits on the specifier: two
 * spellings of the same dynamic import are two chances for one of them to drift
 * (a different path, a `?raw`, an added static import alongside) and quietly
 * pull the renderer back onto the critical path. One expression, one chunk.
 */
export const loadMarkdownDoc = () => import('./MarkdownDoc')

/**
 * Start fetching the renderer before anything needs it.
 *
 * Called when a transcript mounts. Without it the chunk is requested by the
 * first `<Markdown>` to render — i.e. exactly when the user is waiting to read
 * it, which is the worst possible moment for a round trip. Never rejects (a
 * chunk that will not load is a deploy in progress, and the Suspense fallback
 * keeps the transcript readable either way), so a caller may await it bare.
 */
export function prefetchMarkdown(): Promise<void> {
  return loadMarkdownDoc().then(() => undefined, () => undefined)
}
