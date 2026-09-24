import { beforeAll, describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AssistantText } from './AssistantText'
import { prefetchMarkdown } from './markdownChunk'

vi.mock('highlight.js/styles/github-dark.css', () => ({}))


// The markdown renderer is loaded lazily (#1411). In the browser that is a chunk
// fetch; in vitest it is a module TRANSFORM of react-markdown + highlight.js +
// KaTeX, which under a loaded worker can take longer than findBy's default
// timeout. Warming it once here means these tests assert on rendering, not on
// how busy the machine is — the same call the transcript makes on mount.
beforeAll(() => prefetchMarkdown())

describe('AssistantText', () => {
  beforeEach(() => { vi.clearAllMocks() })

  // `<Markdown>` defers its renderer (#1411): the source text is on screen
  // synchronously, the rendered form one microtask later — hence findBy.
  it('renders Markdown while streaming', async () => {
    render(<AssistantText text="**bold** text" streaming={true} />)
    expect((await screen.findByText('bold')).tagName).toBe('STRONG')
  })

  it('keeps streamed Markdown formatting', async () => {
    render(<AssistantText text="**bold**" streaming={true} />)
    // Streaming and settled text share the renderer.
    await screen.findByText('bold')
    expect(document.querySelector('strong')).not.toBeNull()
  })

  it('hides a harness marker while streaming', () => {
    // The settled render drops it (react-markdown, no rehype-raw); the streaming
    // branch used to print it verbatim at the tail of every completed session.
    render(<AssistantText text={'all set\n<!-- viberpit:complete -->'} streaming={true} />)
    expect(screen.getByText('all set')).toBeInTheDocument()
    expect(screen.queryByText(/viberpit:complete/)).toBeNull()
  })

  it('does not render a harness marker once settled either', async () => {
    render(<AssistantText text={'all set\n<!-- viberpit:complete -->'} streaming={false} />)
    expect(await screen.findByText('all set')).toBeInTheDocument()
    expect(screen.queryByText(/viberpit:complete/)).toBeNull()
  })

  it('renders markdown as HTML when not streaming', async () => {
    render(<AssistantText text="**bold text**" streaming={false} />)
    // ReactMarkdown renders <strong>
    const strong = await screen.findByText('bold text')
    expect(strong.tagName).toBe('STRONG')
  })

  it('renders code blocks when not streaming', async () => {
    render(<AssistantText text={'```\nconst x = 1\n```'} streaming={false} />)
    const code = document.querySelector('code')
    expect(code).not.toBeNull()
  })

  it('renders an embedded ★ Insight block as a callout, not literal dashes', async () => {
    const text = [
      'intro line',
      '★ Insight ─────────────────────────────────────',
      'a key point',
      '─────────────────────────────────────────────────',
    ].join('\n')
    render(<AssistantText text={text} streaming={false} />)
    expect(await screen.findByText('Insight')).toBeInTheDocument()
    expect(await screen.findByText('a key point')).toBeInTheDocument()
    // The box-drawing rule lines are not rendered as visible content.
    expect(screen.queryByText(/─────/)).toBeNull()
  })

  it('renders plain settled text without an insight callout', () => {
    render(<AssistantText text="just prose" streaming={false} />)
    expect(screen.getByText('just prose')).toBeInTheDocument()
    expect(screen.queryByText('Insight')).toBeNull()
  })

  it('opens markdown links in a new tab with a safe rel', async () => {
    render(<AssistantText text="[example](https://example.com)" streaming={false} />)
    const link = await screen.findByRole('link', { name: 'example' })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel') ?? '').toContain('noopener')
    expect(link.getAttribute('rel') ?? '').toContain('noreferrer')
  })

  it('renders a path-like inline code span as a clickable copy target', async () => {
    render(<AssistantText text={'see `web/src/foo.tsx:60` here'} streaming={false} />)
    const el = await screen.findByText('web/src/foo.tsx:60')
    expect(el.tagName).toBe('CODE')
    expect(el).toHaveAttribute('role', 'button')
    expect(el).toHaveAttribute('title', 'Copy path')
  })

  it('leaves a non-path inline code span as a plain code element', async () => {
    render(<AssistantText text={'run `npm install` now'} streaming={false} />)
    const el = await screen.findByText('npm install')
    expect(el.tagName).toBe('CODE')
    expect(el).not.toHaveAttribute('role')
  })

  it('does not turn a fenced code block of a single path into a copy button', async () => {
    const { container } = render(
      <AssistantText text={'```\nweb/src/foo.tsx:60\n```'} streaming={false} />,
    )
    // It must render as a plain block <code> inside <pre>, never a CopyablePath.
    expect(container.querySelector('code[role="button"]')).toBeNull()
    expect(container.querySelector('pre code')).not.toBeNull()
  })
})
