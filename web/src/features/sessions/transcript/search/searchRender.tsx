/**
 * The search kit's renderers (D-11, lazy): plain-text marks, the markdown pass, and
 * the lead-run count the current hit is placed by. Reached only through
 * `TranscriptSearchContext`'s `kit`, so none of this is in the eager bundle.
 */
import type { ReactNode } from 'react'
import { highlightSegments, maskMarkdownHidden } from './highlightRanges'
import { rehypeSearchHighlight } from './rehypeSearchHighlight'
import { MARK_CLASS, MARK_CURRENT_ATTR, MARK_CURRENT_CLASS } from './searchMarks'
import type { RehypePluginEntry, SearchHighlight, SearchRenderKit } from './TranscriptSearchContext'

function renderText(text: string, highlight: SearchHighlight): ReactNode {
  if (highlight.terms.length === 0) return text
  const segs = highlightSegments(text, highlight.terms)
  const runs = segs.filter((s) => s.lead).length
  const target = highlight.currentLead === null ? -1 : Math.min(highlight.currentLead, runs - 1)
  let lead = -1
  return segs.map((seg, i) => {
    if (!seg.mark) return seg.text
    if (seg.lead) lead++
    return seg.lead && lead === target
      ? <mark key={i} className={MARK_CURRENT_CLASS} {...{ [MARK_CURRENT_ATTR]: 'true' }}>{seg.text}</mark>
      : <mark key={i} className={MARK_CLASS}>{seg.text}</mark>
  })
}

/** How many lead-term runs of `text` render marked: markdown code is skipped. */
export function leadRuns(text: string, terms: readonly string[], markdown: boolean): number {
  let n = 0
  for (const seg of highlightSegments(markdown ? maskMarkdownHidden(text) : text, terms)) if (seg.lead) n++
  return n
}

function splitHighlight(
  texts: readonly string[],
  terms: readonly string[],
  lead: number | null,
  markdown: boolean,
): SearchHighlight[] {
  const runs = texts.map((t) => leadRuns(t, terms, markdown))
  let at = -1
  let local: number | null = null
  if (lead !== null) {
    let left = lead
    for (let i = 0; i < texts.length && at === -1; i++) {
      if (left < runs[i]) {
        at = i
        local = left
      } else {
        left -= runs[i]
      }
    }
    if (at === -1) {
      // Past the turn's last rendered run: the last one is current.
      for (let i = texts.length - 1; i >= 0 && at === -1; i--) {
        if (runs[i] > 0) {
          at = i
          local = runs[i] - 1
        }
      }
    }
  }
  return texts.map((_, i) => ({ terms, currentLead: i === at ? local : null, kit: renderKit }))
}

const rehypePlugin = (highlight: SearchHighlight): RehypePluginEntry => [rehypeSearchHighlight, highlight]

export const renderKit: SearchRenderKit = { renderText, rehypePlugin, splitHighlight }
