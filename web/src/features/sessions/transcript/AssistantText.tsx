import { parseInsights } from './insights'
import { hideHtmlComments } from './htmlComments'
import { InsightCallout } from './InsightCallout'
import { Markdown } from './Markdown'

interface Props { text: string; streaming: boolean }

export function AssistantText({ text, streaming }: Props) {
  // Also hide incomplete harness comments as their characters arrive.
  const visible = hideHtmlComments(text)

  if (streaming) return <Markdown streaming>{visible}</Markdown>

  const segments = parseInsights(visible)
  return (
    <>
      {segments.map((seg, i) =>
        seg.kind === 'insight' ? (
          <InsightCallout key={i} text={seg.text} />
        ) : (
          <Markdown key={i}>{seg.text}</Markdown>
        ),
      )}
    </>
  )
}
