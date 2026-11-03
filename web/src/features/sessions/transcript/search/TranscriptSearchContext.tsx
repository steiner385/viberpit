/* eslint-disable react-refresh/only-export-components -- the context, its value
   type and the one renderer that reads a block's highlight are one contract. */
/**
 * The transcript search's render-time highlight (D-11).
 *
 * `TranscriptView` provides the live query's terms and the turn holding the
 * current hit; `Turn` reads them and hands each prose block a `SearchHighlight`.
 * Only prose is highlighted — user prompts and assistant text/markdown. A tool
 * row re-provides `null` (`Turn`), so a sub-agent transcript nested in a tool card
 * never highlights: the server's search excludes sub-agent events, and a mark
 * there would be a hit the counter never counted.
 */
import { createContext } from 'react'
import { highlightSegments } from './highlightRanges'

export interface TranscriptSearchState {
  /** The query's terms (`parseSearchTerms`), non-empty. */
  terms: readonly string[]
  /** The id of the rendered turn the current hit scrolled to, if any. */
  currentTurnId: string | null
}

/** Null: no search is open, or this subtree is not searchable prose. */
export const TranscriptSearchContext = createContext<TranscriptSearchState | null>(null)

/** One prose block's highlight: the terms, and whether it holds the current hit. */
export interface SearchHighlight {
  terms: readonly string[]
  /** This block holds the current hit: its first lead-term mark is styled stronger. */
  current: boolean
}

/** Every match: a translucent warning tone, so it shows on every prose surface — the
 *  canvas, the accent-washed user bubble, the info-washed automated prompt and the
 *  warning-washed insight callout alike — in both themes. */
export const MARK_CLASS = 'rounded-sm bg-warning/30 text-ink'
/** The current hit: the solid strong tone with its on-strong ink (AA), so it stands
 *  out among the other marks. */
export const MARK_CURRENT_CLASS = 'rounded-sm bg-warning-strong text-on-strong'
/** Set on the current hit's `<mark>`, for tests and e2e to find it. */
export const MARK_CURRENT_ATTR = 'data-search-current'

/** Plain text with the search's marks — the user bubble, an automated prompt. */
export function HighlightedText({ text, highlight }: { text: string; highlight?: SearchHighlight }) {
  if (!highlight || highlight.terms.length === 0) return <>{text}</>
  const segs = highlightSegments(text, highlight.terms)
  const currentAt = highlight.current ? segs.findIndex((s) => s.mark && s.lead) : -1
  return (
    <>
      {segs.map((seg, i) => {
        if (!seg.mark) return seg.text
        return i === currentAt
          ? <mark key={i} className={MARK_CURRENT_CLASS} {...{ [MARK_CURRENT_ATTR]: 'true' }}>{seg.text}</mark>
          : <mark key={i} className={MARK_CLASS}>{seg.text}</mark>
      })}
    </>
  )
}
