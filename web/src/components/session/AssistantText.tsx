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
    <div className="markdown max-w-none text-gray-900 dark:text-gray-100">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
