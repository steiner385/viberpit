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

  it('opens markdown links in a new tab with a safe rel', async () => {
    render(<AssistantText text="[example](https://example.com)" streaming={false} />)
    const link = await screen.findByRole('link', { name: 'example' })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel') ?? '').toContain('noopener')
    expect(link.getAttribute('rel') ?? '').toContain('noreferrer')
  })
})
