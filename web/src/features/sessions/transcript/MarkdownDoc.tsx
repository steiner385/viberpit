import { createContext, useContext, useId, useMemo, type ComponentProps } from 'react'
import ReactMarkdown, { type Components, type Options } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import 'katex/dist/katex.min.css'
import { MermaidDiagram } from './MermaidDiagram'
import 'highlight.js/styles/github-dark.css'
import { isFilePathRef } from './filePathRef'
import { PathRef } from './PathRef'
import { ResultImage } from './ResultImages'
import { isSafeHttpUrl } from '@/lib/safeUrl'

interface Props {
  children: string
  streaming?: boolean
  /** The transcript search's highlight pass (D-11), run last — built by `Markdown`
   *  (`rehypeSearchHighlight`) so this lazy module imports none of the search code. */
  searchPlugin?: NonNullable<Options['rehypePlugins']>[number]
  /** Third-party text (a PR review comment, D-15): never fetch an image — it
   *  renders as its alt text plus a link — and keep only absolute http(s) links
   *  clickable, so a relative `/api/…` link cannot act on the ViberPit origin. */
  untrusted?: boolean
}

const ImageLinkContext = createContext(false)

/** An untrusted image, never fetched: its alt text, linked to its url when that
 *  is http(s) and it is not already inside a link. */
function UntrustedImage({ src, alt }: ComponentProps<'img'>) {
  const linked = useContext(ImageLinkContext)
  const label = alt || 'image'
  if (linked || !isSafeHttpUrl(typeof src === 'string' ? src : null)) return <span>[{label}]</span>
  return <a href={src} target="_blank" rel="noopener noreferrer nofollow">[{label}]</a>
}

function MarkdownImage({ src, alt }: ComponentProps<'img'>) {
  const linked = useContext(ImageLinkContext)
  return <ResultImage url={typeof src === 'string' ? src : ''} name={alt || ''} linked={linked} />
}

// Shared markdown renderer for assistant transcript content (prose and insight
// callouts). Centralizes the plugin set, the safe-link policy, and clickable
// file-path code spans.
//
// Reached only through `./Markdown`, which loads this module lazily (#1411): the
// remark/rehype chain, highlight.js and KaTeX below are ~240kB gzip and nothing
// outside a transcript needs them. Import this file directly and that whole
// chain lands back in the initial payload — `npm run budget` is what says so.
export function MarkdownDoc({ children, streaming = false, searchPlugin, untrusted = false }: Props) {
  // Sanitized IDs are unique across messages; local links follow the same prefix.
  const idPrefix = `markdown-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}-`
  const schema = {
    ...defaultSchema,
    clobberPrefix: idPrefix,
    attributes: {
      ...defaultSchema.attributes,
      code: [['className', /^language-./, 'math-inline', 'math-display']],
    },
  }
  // Stable component identities preserve mounted diagrams when transcript
  // measurement or live updates rerender this message.
  const components = useMemo<Components>(() => ({
    // Open links in a new tab and sever the opener relationship so a
    // session link can't navigate the transcript tab away or reach
    // window.opener.
    a: ({ node, ...props }) => {
      void node // hast node — drop it rather than forward to the DOM
      // Untrusted text: anything but an absolute http(s) link renders as its text.
      if (untrusted && !isSafeHttpUrl(props.href)) {
        return <ImageLinkContext.Provider value={true}><span>{props.children}</span></ImageLinkContext.Provider>
      }
      const local = props.href?.startsWith('#')
      return <ImageLinkContext.Provider value={true}>
        <a {...props} href={local ? `#${idPrefix}${props.href!.slice(1)}` : props.href}
          target={local ? undefined : '_blank'} rel="noopener noreferrer" />
      </ImageLinkContext.Provider>
    },
    img: untrusted ? UntrustedImage : MarkdownImage,
    pre: ({ node, children, ...props }) => {
      const code = node?.children[0]
      if (!streaming && code?.type === 'element' && code.tagName === 'code'
        && Array.isArray(code.properties.className) && code.properties.className.includes('language-mermaid')) {
        const source = code.children.map((child) => child.type === 'text' ? child.value : '').join('')
        return <MermaidDiagram source={source} />
      }
      return <pre {...props}>{children}</pre>
    },
    // Make path-like INLINE code spans clickable: a link to the Files tab under
    // the live session's cwd, else click-to-copy (see `PathRef`). Fenced blocks
    // arrive as highlighted element arrays (not a string), so the
    // typeof guard restricts this to inline code only — no `inline`
    // prop exists in react-markdown v10.
    code: ({ node, children, ...props }) => {
      void node
      if (typeof children === 'string' && isFilePathRef(children)) {
        return <PathRef text={children} />
      }
      return <code {...props}>{children}</code>
    },
  }), [idPrefix, streaming, untrusted])
  // Sanitize author HTML before trusted math/highlighting generate markup. The
  // search marks (D-11) go last: they wrap the final tree's text nodes, skipping
  // code and math (`rehypeSearchHighlight`).
  const rehypePlugins: NonNullable<Options['rehypePlugins']> = [rehypeRaw, [rehypeSanitize, schema], rehypeKatex, rehypeHighlight]
  if (searchPlugin) rehypePlugins.push(searchPlugin)
  return (
    <div className="markdown max-w-none text-ink">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={rehypePlugins}
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
