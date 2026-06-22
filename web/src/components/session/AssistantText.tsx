import { parseInsights } from './insights'
import { InsightCallout } from './InsightCallout'
import { Markdown } from './Markdown'

interface Props { text: string; streaming: boolean }

export function AssistantText({ text, streaming }: Props) {
  if (streaming) {
    return (
      <div className="whitespace-pre-wrap text-sm text-gray-900 dark:text-gray-100 font-mono">
        {text}
      </div>
    )
  }

  const segments = parseInsights(text)
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
