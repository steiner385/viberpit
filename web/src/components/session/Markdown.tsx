import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'
import { isFilePathRef } from './filePathRef'
import { CopyablePath } from './CopyablePath'

interface Props { children: string }

// Shared markdown renderer for assistant transcript content (prose and insight
// callouts). Centralizes the plugin set, the safe-link policy, and clickable
// file-path code spans.
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
          // Make path-like INLINE code spans clickable to copy. Fenced blocks
          // arrive as highlighted element arrays (not a string), so the
          // typeof guard restricts this to inline code only — no `inline`
          // prop exists in react-markdown v10.
          code: ({ node, children, ...props }) => {
            void node
            if (typeof children === 'string' && isFilePathRef(children)) {
              return <CopyablePath text={children} />
            }
            return <code {...props}>{children}</code>
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
