import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { WebResult } from '../WebResult'

describe('WebResult', () => {
  it('shows the fetched URL and renders the body as markdown', () => {
    render(
      <WebResult
        name="WebFetch"
        input={{ url: 'https://example.com/docs' }}
        payload={{ content: 'See the **guide**.' }}
      />,
    )
    expect(screen.getByText('https://example.com/docs')).toBeInTheDocument()
    expect(screen.getByText('guide').tagName).toBe('STRONG')
  })

  it('shows the search query for WebSearch', () => {
    render(
      <WebResult
        name="WebSearch"
        input={{ query: 'opus pricing' }}
        payload={{ content: 'result text' }}
      />,
    )
    expect(screen.getByText(/opus pricing/)).toBeInTheDocument()
    expect(screen.getByText(/result text/)).toBeInTheDocument()
  })

  it('never leaks the raw envelope JSON', () => {
    render(<WebResult name="WebFetch" input={{ url: 'https://x.io' }} payload={{ content: 'body' }} />)
    expect(screen.queryByText(/"content"/)).toBeNull()
  })
})
