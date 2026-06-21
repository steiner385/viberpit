import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { InsightCallout } from '../InsightCallout'

vi.mock('highlight.js/styles/github-dark.css', () => ({}))

describe('InsightCallout', () => {
  it('renders the ★ Insight header', () => {
    render(<InsightCallout text="a point" />)
    expect(screen.getByText('Insight')).toBeInTheDocument()
  })

  it('renders the body as markdown', async () => {
    render(<InsightCallout text="**bold** point" />)
    const strong = await screen.findByText('bold')
    expect(strong.tagName).toBe('STRONG')
  })
})
