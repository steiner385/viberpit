import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
// Tests the RENDERER directly, not the `./Markdown` boundary that loads it
// lazily (#1411): the plugin chain, the sanitiser and the mermaid dispatch are
// what these assert, and going through the Suspense wrapper would make every
// one of them wait on a chunk to prove something about markdown.
// `Markdown.test.tsx` covers the boundary itself.
import { MarkdownDoc as Markdown } from './MarkdownDoc'

vi.mock('./MermaidDiagram', () => ({
  MermaidDiagram: ({ source }: { source: string }) => <figure aria-label="Mermaid diagram">{source}</figure>,
}))

describe('Markdown', () => {
  it('renders Mermaid fences as diagrams, but keeps inline Mermaid text and other fences as code', () => {
    const { container } = render(<Markdown>{'```mermaid\ngraph TD; A-->B\n```\n\n`mermaid`\n\n```text\nplain code\n```'}</Markdown>)
    expect(screen.getByRole('figure', { name: 'Mermaid diagram' })).toHaveTextContent('graph TD; A-->B')
    expect(container.querySelector('pre code')).toHaveTextContent('plain code')
    expect(screen.getByText('mermaid').tagName).toBe('CODE')
  })

  it('keeps streaming Mermaid source readable until the block settles', () => {
    const { rerender } = render(<Markdown streaming>{'```mermaid\ngraph TD; A-->'}</Markdown>)
    expect(screen.queryByRole('figure')).toBeNull()
    expect(screen.getByText('graph TD; A-->')).toBeInTheDocument()
    rerender(<Markdown>{'```mermaid\ngraph TD; A-->B\n```'}</Markdown>)
    expect(screen.getByRole('figure')).toBeInTheDocument()
  })

  it('renders CommonMark, GFM, images, and footnotes with working local anchors', () => {
    const { container } = render(<Markdown>{'# Heading\n\n**bold** *italic* ~~gone~~\n\n> quote\n\n1. first\n   - nested\n\n- [x] done\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n![alt](https://example.com/image.png)\n\nA note[^1].\n\n[^1]: Footnote\n\n---'}</Markdown>)
    expect(screen.getByRole('heading', { name: 'Heading' })).toBeInTheDocument()
    for (const tag of ['strong', 'em', 'del', 'blockquote', 'ol', 'ul', 'table', 'hr']) expect(container.querySelector(tag)).not.toBeNull()
    expect(screen.getByRole('checkbox')).toBeChecked()
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(screen.getByRole('img', { name: 'alt' })).toBeInTheDocument()
    const ref = container.querySelector('a[data-footnote-ref]')!
    expect(ref).not.toHaveAttribute('target')
    expect(container.querySelector(`[id="${ref.getAttribute('aria-describedby')}"]`)).not.toBeNull()
    expect(container.querySelector(`[id="${ref.getAttribute('href')!.slice(1)}"]`)).not.toBeNull()
  })

  it('renders inline and display math', () => {
    const { container } = render(<Markdown>{'Inline $x^2$\n\n$$\nx + y = z\n$$'}</Markdown>)
    expect(container.querySelectorAll('.katex')).toHaveLength(2)
    expect(container.querySelector('.katex-display')).not.toBeNull()
  })

  it('renders safe HTML and strips executable HTML and unsafe links', () => {
    const { container } = render(<Markdown>{'<details><summary>More</summary><kbd>Enter</kbd><sup>2</sup></details>\n\n<script>alert(1)</script><iframe src="https://evil.example"></iframe><img src="x" onerror="alert(1)">\n\n[bad](javascript:alert%281%29)'}</Markdown>)
    expect(screen.getByText('More').tagName).toBe('SUMMARY')
    expect(screen.getByText('Enter').tagName).toBe('KBD')
    expect(container.querySelector('script, iframe, [onerror]')).toBeNull()
    expect(screen.getByText('bad')).not.toHaveAttribute('href', expect.stringContaining('javascript:'))
  })
})

describe('workspace screenshots', () => {
  it.each(['/home/user/worktree/screenshot.png', './docs/screenshot.png', 'file:///tmp/screenshot.png'])('does not fetch workspace image %s as a browser URL', (path) => {
    render(<Markdown>{`![Settings screenshot](${path})`}</Markdown>)
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByText(/share_artifact/)).toHaveTextContent('Image unavailable')
    expect(screen.getByText(/Settings screenshot/)).toBeVisible()
  })

  it('renders canonical attachment images', () => {
    render(<Markdown>{'![Screenshot](/api/attachments/abc)'}</Markdown>)
    expect(screen.getByRole('img')).toHaveAttribute('src', '/api/attachments/abc')
  })
})

describe('linked screenshots', () => {
  it('preserves an image link destination without nesting anchors', () => {
    const { container } = render(<Markdown>{'[![Screenshot](/api/attachments/abc)](https://example.com/report)'}</Markdown>)
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://example.com/report')
    expect(container.querySelector('a a')).toBeNull()
    expect(screen.getByRole('img')).toHaveAttribute('src', '/api/attachments/abc')
  })
})
