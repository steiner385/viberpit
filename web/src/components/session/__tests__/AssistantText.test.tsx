import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AssistantText } from '../AssistantText'

vi.mock('highlight.js/styles/github-dark.css', () => ({}))

describe('AssistantText', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders raw text (incl. markdown chars) when streaming', () => {
    render(<AssistantText text="**bold** text" streaming={true} />)
    expect(screen.getByText('**bold** text')).toBeInTheDocument()
  })

  it('does not render markdown when streaming', () => {
    render(<AssistantText text="**bold**" streaming={true} />)
    // No <strong> element
    expect(document.querySelector('strong')).toBeNull()
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
