import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'

interface Props { text: string }

export function InsightCallout({ text }: Props) {
  return (
    <div className="my-3 rounded-r border-l-[3px] border-amber-400 bg-amber-50 px-3.5 py-3 dark:bg-amber-400/10">
      <div className="mb-1.5 flex items-center gap-1.5 text-[13px] font-semibold text-amber-700 dark:text-amber-300">
        <span aria-hidden="true">★</span> Insight
      </div>
      <div className="prose prose-sm dark:prose-invert max-w-none">
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
          {text}
        </ReactMarkdown>
      </div>
    </div>
  )
}
