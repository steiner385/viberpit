import { Suspense, lazy } from 'react'
import { loadMarkdownDoc } from './markdownChunk'

/**
 * The transcript's markdown renderer, loaded off the critical path (#1411).
 *
 * `MarkdownDoc` is the real implementation and by far the heaviest thing in the
 * app after the HEIC decoder: react-markdown, the remark/rehype chain,
 * highlight.js and KaTeX come to ~240kB gzip. None of it is needed to render the
 * sessions list, the welcome pane, Roadmap, Delivery or Settings — only a
 * transcript uses it — yet a static import put all of it in the preloaded set
 * that every page load fetches before it can paint. Splitting it here is what
 * took the initial payload from 484kB to 256kB gzip.
 *
 * ## The fallback is the source text, not a spinner
 *
 * A transcript is prose that is already in hand; the chunk only changes how it
 * is FORMATTED. So while it loads, the same text renders as plain text in the
 * same box — the message is readable immediately and sharpens into rendered
 * markdown, rather than the user watching a placeholder where their answer
 * should be. It also keeps the layout shift small: the fallback occupies roughly
 * the height the rendered version will, and CLS is one of the three numbers
 * #1411 is trying to hold down.
 *
 * In practice the fallback is rarely seen at all: `prefetchMarkdown()`
 * (`./markdownChunk`) runs when a transcript mounts, so the chunk is usually
 * warm before the first message arrives.
 */
const MarkdownDoc = lazy(() => loadMarkdownDoc().then((m) => ({ default: m.MarkdownDoc })))

interface Props { children: string; streaming?: boolean }

export function Markdown({ children, streaming = false }: Props) {
  return (
    <Suspense fallback={<div className="whitespace-pre-wrap break-words">{children}</div>}>
      <MarkdownDoc streaming={streaming}>{children}</MarkdownDoc>
    </Suspense>
  )
}
