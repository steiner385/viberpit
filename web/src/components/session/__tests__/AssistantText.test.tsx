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
})
