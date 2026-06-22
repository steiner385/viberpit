import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'

interface Props { children: string }

// Shared markdown renderer for assistant transcript content (prose and insight
// callouts). Centralizes the plugin set and the safe-link policy.
export function Markdown({ children }: Props) {
  return (
    <div className="markdown max-w-none text-gray-900 dark:text-gray-100">
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
        {children}
      </ReactMarkdown>
    </div>
  )
}
