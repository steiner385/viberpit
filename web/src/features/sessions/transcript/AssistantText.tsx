import { parseInsights } from './insights'
import { hideHtmlComments } from './htmlComments'
import { InsightCallout } from './InsightCallout'
import { Markdown } from './Markdown'
import { splitHighlight, type SearchHighlight } from './search/TranscriptSearchContext'

interface Props {
  text: string
  streaming: boolean
  /** The transcript search's marks (D-11). */
  highlight?: SearchHighlight
}

export function AssistantText({ text, streaming, highlight }: Props) {
  // Also hide incomplete harness comments as their characters arrive.
  const visible = hideHtmlComments(text)

  if (streaming) return <Markdown streaming highlight={highlight}>{visible}</Markdown>

  const segments = parseInsights(visible)
  // The block's current lead-term run, re-counted into the segment holding it.
  const split = highlight
    && splitHighlight(segments.map((s) => s.text), highlight.terms, highlight.currentLead, highlight.kit)
  const segHighlight = (i: number): SearchHighlight | undefined => (split ? split[i] : undefined)
  return (
    <>
      {segments.map((seg, i) =>
        seg.kind === 'insight' ? (
          <InsightCallout key={i} text={seg.text} highlight={segHighlight(i)} />
        ) : (
          <Markdown key={i} highlight={segHighlight(i)}>{seg.text}</Markdown>
        ),
      )}
    </>
  )
}
