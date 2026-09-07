import { parseInsights } from './insights'
import { hideHtmlComments } from './htmlComments'
import { InsightCallout } from './InsightCallout'
import { Markdown } from './Markdown'

interface Props { text: string; streaming: boolean }

export function AssistantText({ text, streaming }: Props) {
  // Both branches, not just the live one: react-markdown does not DROP a raw HTML
  // comment, it escapes it into visible text, so a harness marker is readable in the
  // settled transcript as well as while it streams. See `hideHtmlComments`.
  const visible = hideHtmlComments(text)

  if (streaming) {
    return (
      <div className="whitespace-pre-wrap text-sm text-gray-900 dark:text-gray-100 font-mono">
        {visible}
      </div>
    )
  }

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
