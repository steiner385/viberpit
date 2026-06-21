import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'
import { parseInsights } from './insights'
import { InsightCallout } from './InsightCallout'

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
          <div key={i} className="prose prose-sm dark:prose-invert max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
              {seg.text}
            </ReactMarkdown>
          </div>
        ),
      )}
    </>
  )
}
