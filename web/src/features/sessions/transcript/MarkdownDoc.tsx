import { createContext, useContext, useId, useMemo, type ComponentProps, type ReactNode } from 'react'
import { Link, useInRouterContext } from 'react-router-dom'
import ReactMarkdown, { defaultUrlTransform, type Components, type Options, type UrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import 'katex/dist/katex.min.css'
import { MermaidDiagram } from './MermaidDiagram'
import 'highlight.js/styles/github-dark.css'
import { isFilePathRef, localPathTarget, worktreeRelativePath, type FilePathRef } from './filePathRef'
import { useFileRefActions } from './fileRefContext'
import { PathRef } from './PathRef'
import { ResultImage } from './ResultImages'
import { WorktreeImage } from './WorktreeImage'
import { isSafeHttpUrl } from '@/lib/safeUrl'
import { useIsAppRoute } from '@/shell/appRouteContext'

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

/** True inside a link: an image or code span there is part of the link's one
 *  click target, never a control of its own. */
const ImageLinkContext = createContext(false)

/** react-markdown's default drops any url with a scheme outside http(s)/mailto/…;
 *  trusted text also keeps a `file:` url (never emitted as an href — the `a` and
 *  `img` renderers route it through `localPathTarget`) and a `data:` image src
 *  (`ResultImage` admits only its strict base64 raster form). The sanitize
 *  schema below allows the same two, and only for trusted text. */
const trustedUrlTransform: UrlTransform = (url, key) => {
  if (/^file:/i.test(url)) return url
  if (key === 'src' && /^data:/i.test(url)) return url
  return defaultUrlTransform(url)
}

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
  const url = typeof src === 'string' ? src : ''
  // A path on the session host, not a url the browser can fetch.
  const local = localPathTarget(url)
  if (local) return <WorktreeImage path={local.path} alt={alt || ''} linked={linked} />
  return <ResultImage url={url} name={alt || ''} linked={linked} />
}

/**
 * A link whose href is scheme-less or `file:` — a path, never an href the
 * browser should open as written. Under the live session's worktree it opens
 * in Files; else an absolute href naming a page of this app (`/sessions/<id>`,
 * which the assistant drawer is told to write) is an in-app, same-tab link;
 * anything else is plain text titled with the path (`PathRef`).
 */
function HostPathLink({ href, target, children }: { href: string; target: FilePathRef; children: ReactNode }) {
  const actions = useFileRefActions()
  const inRouter = useInRouterContext()
  const isAppRoute = useIsAppRoute()
  const inWorktree = actions !== null && worktreeRelativePath(target.path, actions.cwd) !== null
  if (!inWorktree && href.startsWith('/') && isAppRoute(href.split(/[?#]/)[0])) {
    return inRouter ? <Link to={href}>{children}</Link> : <a href={href}>{children}</a>
  }
  const text = target.line ? `${target.path}:${target.line}` : target.path
  return <PathRef text={text} target={target}>{children}</PathRef>
}

/** Inline code. A path-like span is a `PathRef` (Files link under the live
 *  session's cwd, else click-to-copy) — except inside a link, whose own target
 *  is the one thing a click does. Fenced blocks arrive as highlighted element
 *  arrays (not a string), so the typeof guard restricts this to inline code
 *  only — no `inline` prop exists in react-markdown v10. */
function MarkdownCode({ node, children, ...props }: ComponentProps<'code'> & { node?: unknown }) {
  void node // hast node — drop it rather than forward to the DOM
  const linked = useContext(ImageLinkContext)
  if (!linked && typeof children === 'string' && isFilePathRef(children)) return <PathRef text={children} />
  return <code {...props}>{children}</code>
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
  const protocols = defaultSchema.protocols ?? {}
  const schema = {
    ...defaultSchema,
    clobberPrefix: idPrefix,
    attributes: {
      ...defaultSchema.attributes,
      code: [['className', /^language-./, 'math-inline', 'math-display']],
    },
    // See `trustedUrlTransform`: untrusted text keeps the default protocols.
    protocols: untrusted ? protocols : {
      ...protocols,
      href: [...(protocols.href ?? []), 'file'],
      src: [...(protocols.src ?? []), 'file', 'data'],
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
      // No href: sanitize dropped an unsafe one. Text, not an anchor that does nothing.
      if (!props.href) return <ImageLinkContext.Provider value={true}><span>{props.children}</span></ImageLinkContext.Provider>
      // A path on the session host (`docs/x.md`, `file:///…`): a relative href
      // would open a dead SPA url and a `file:` one is blocked (see `HostPathLink`).
      const target = untrusted ? null : localPathTarget(props.href)
      if (target) {
        return <ImageLinkContext.Provider value={true}><HostPathLink href={props.href} target={target}>{props.children}</HostPathLink></ImageLinkContext.Provider>
      }
      // A `file:` url that is not a local path (`file://host/share`, unparseable):
      // still never an href — text.
      if (/^file:/i.test(props.href)) {
        return <ImageLinkContext.Provider value={true}><span>{props.children}</span></ImageLinkContext.Provider>
      }
      const local = props.href.startsWith('#')
      return <ImageLinkContext.Provider value={true}>
        <a {...props} href={local ? `#${idPrefix}${props.href.slice(1)}` : props.href}
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
    code: MarkdownCode,
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
        urlTransform={untrusted ? defaultUrlTransform : trustedUrlTransform}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
