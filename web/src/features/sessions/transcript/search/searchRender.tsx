/**
 * The search kit's renderers (D-11, lazy): plain-text marks, the markdown pass, and
 * the lead-run count the current hit is placed by. Reached only through
 * `TranscriptSearchContext`'s `kit`, so none of this is in the eager bundle.
 */
import type { ReactNode } from 'react'
import { highlightSegments } from './highlightRanges'
import { rehypeSearchHighlight } from './rehypeSearchHighlight'
import { MARK_CLASS, MARK_CURRENT_ATTR, MARK_CURRENT_CLASS } from './searchMarks'
import type { RehypePluginEntry, SearchHighlight, SearchRenderKit } from './TranscriptSearchContext'

function renderText(text: string, highlight: SearchHighlight): ReactNode {
  if (highlight.terms.length === 0) return text
  let lead = -1
  return highlightSegments(text, highlight.terms).map((seg, i) => {
    if (!seg.mark) return seg.text
    if (seg.lead) lead++
    return seg.lead && lead === highlight.currentLead
      ? <mark key={i} className={MARK_CURRENT_CLASS} {...{ [MARK_CURRENT_ATTR]: 'true' }}>{seg.text}</mark>
      : <mark key={i} className={MARK_CLASS}>{seg.text}</mark>
  })
}

function leadRuns(text: string, terms: readonly string[]): number {
  let n = 0
  for (const seg of highlightSegments(text, terms)) if (seg.lead) n++
  return n
}

const rehypePlugin = (highlight: SearchHighlight): RehypePluginEntry => [rehypeSearchHighlight, highlight]

export const renderKit: SearchRenderKit = { renderText, rehypePlugin, leadRuns }
