/**
 * The markdown half of the transcript search's highlight (D-11, lazy): a rehype
 * pass that wraps matches in the rendered markdown's TEXT nodes in `<mark>`.
 *
 * It runs last — after sanitize, KaTeX and highlight.js — so it sees the final
 * tree, and the marks it adds are its own trusted markup. Code (fenced or inline)
 * is never touched: a fenced block's text is highlight.js's token spans, and an
 * inline code span must stay a bare string for `PathRef` to recognise a path. Math
 * (KaTeX's spans), SVG and scripts are skipped too. The current hit is the
 * `currentLead`-th lead-term run in document order.
 */
import type { Element, ElementContent, Root, RootContent } from 'hast'
import { highlightSegments } from './highlightRanges'
import { MARK_CLASS, MARK_CURRENT_ATTR, MARK_CURRENT_CLASS } from './searchMarks'
import type { SearchHighlight } from './TranscriptSearchContext'

const SKIP_TAGS = new Set(['pre', 'code', 'kbd', 'samp', 'script', 'style', 'svg', 'math'])

function skipped(el: Element): boolean {
  if (SKIP_TAGS.has(el.tagName)) return true
  const cls = el.properties?.className
  return Array.isArray(cls) && cls.some((c) => typeof c === 'string' && c.startsWith('katex'))
}

export function rehypeSearchHighlight(highlight: SearchHighlight) {
  return (tree: Root) => {
    if (highlight.terms.length === 0) return
    let lead = -1

    function mark(text: string, current: boolean): Element {
      return {
        type: 'element',
        tagName: 'mark',
        properties: current
          ? { className: MARK_CURRENT_CLASS.split(' '), [MARK_CURRENT_ATTR]: 'true' }
          : { className: MARK_CLASS.split(' ') },
        children: [{ type: 'text', value: text }],
      }
    }

    function visit(parent: Root | Element): void {
      const next: Array<RootContent | ElementContent> = []
      for (const child of parent.children) {
        if (child.type === 'element') {
          if (!skipped(child)) visit(child)
          next.push(child)
        } else if (child.type === 'text') {
          const segs = highlightSegments(child.value, highlight.terms)
          if (segs.length === 1 && !segs[0].mark) {
            next.push(child)
            continue
          }
          for (const seg of segs) {
            if (!seg.mark) {
              next.push({ type: 'text', value: seg.text })
              continue
            }
            if (seg.lead) lead++
            next.push(mark(seg.text, seg.lead && lead === highlight.currentLead))
          }
        } else {
          next.push(child)
        }
      }
      parent.children = next as typeof parent.children
    }

    visit(tree)
  }
}
