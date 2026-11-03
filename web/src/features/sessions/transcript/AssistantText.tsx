import { parseInsights } from './insights'
import { hideHtmlComments } from './htmlComments'
import { InsightCallout } from './InsightCallout'
import { Markdown } from './Markdown'
import type { SearchHighlight } from './search/TranscriptSearchContext'

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
  // The current hit's stronger mark belongs to the FIRST segment holding the lead
  // term; every other segment gets plain marks.
  const lead = highlight?.current ? highlight.terms[0] : undefined
  const currentSeg = lead === undefined ? -1 : segments.findIndex((s) => s.text.toLowerCase().includes(lead))
  const segHighlight = (i: number): SearchHighlight | undefined =>
    highlight && { terms: highlight.terms, current: i === currentSeg }
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
