import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'

interface Props { text: string; streaming: boolean }

export function AssistantText({ text, streaming }: Props) {
  if (streaming) {
    return (
      <div className="whitespace-pre-wrap text-sm text-gray-900 dark:text-gray-100 font-mono">
        {text}
      </div>
    )
  }
  return (
    <div className="prose prose-sm dark:prose-invert max-w-none">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // Open links in a new tab and sever the opener relationship so a
          // session link can't navigate the transcript tab away or reach
          // window.opener.
          a: ({ node, ...props }) => {
            void node // hast node — drop it rather than forward to the DOM
            return <a {...props} target="_blank" rel="noopener noreferrer" />
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
