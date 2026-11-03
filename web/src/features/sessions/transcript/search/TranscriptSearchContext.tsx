/**
 * The transcript search's render-time highlight (D-11) — the EAGER half.
 *
 * Only the context and its types live here: every function that finds or draws a
 * match (`highlightRanges`, the plain-text and markdown renderers, the jump) is in
 * the lazy search kit (`searchKit`), loaded with the ⌘F bar and handed down through
 * this context as `kit`. A transcript nobody searches never downloads any of it;
 * the eager renderers only null-check the context.
 *
 * `TranscriptView` provides the query's terms, the current hit and the kit; `Turn`
 * reads them and hands each prose block a `SearchHighlight`. Only prose is
 * highlighted — user prompts and assistant text/markdown. A tool row re-provides
 * `null` (`Turn`), so a sub-agent transcript nested in a tool card never
 * highlights: the server's search excludes sub-agent events.
 */
import { createContext, type ReactNode } from 'react'
import type { Options } from 'react-markdown'

/** A rehype plugin entry, as `MarkdownDoc` takes it. */
export type RehypePluginEntry = NonNullable<Options['rehypePlugins']>[number]

/** The search kit's render functions (`searchRender`), reached through the context. */
export interface SearchRenderKit {
  /** Plain text with its marks. */
  renderText: (text: string, highlight: SearchHighlight) => ReactNode
  /** The markdown text-node pass for `highlight`. */
  rehypePlugin: (highlight: SearchHighlight) => RehypePluginEntry
  /** A turn-level current lead split across consecutive prose texts: each text's
   *  highlight, in order. Counted the way the marks render — markdown code is
   *  skipped when `markdown`; a lead past the last run falls back to the last. */
  splitHighlight: (
    texts: readonly string[],
    terms: readonly string[],
    lead: number | null,
    markdown: boolean,
  ) => SearchHighlight[]
}

/** The current hit: the rendered turn it landed on, and which lead-term run in that
 *  turn (0-based, across its prose blocks in order) is the match. */
export interface SearchCurrent {
  turnId: string
  lead: number
}

export interface TranscriptSearchState {
  /** The query's terms, non-empty. */
  terms: readonly string[]
  current: SearchCurrent | null
  kit: SearchRenderKit
}

/** Null: no search is open, or this subtree is not searchable prose. */
export const TranscriptSearchContext = createContext<TranscriptSearchState | null>(null)

/** One prose block's highlight. */
export interface SearchHighlight {
  terms: readonly string[]
  /** Which lead-term run IN THIS BLOCK is the current hit, or null for none. Past
   *  the block's last run, the last run is current. */
  currentLead: number | null
  kit: SearchRenderKit
}
