import { beforeAll, describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { WebResult } from './WebResult'
import { prefetchMarkdown } from './markdownChunk'


// The markdown renderer is loaded lazily (#1411). In the browser that is a chunk
// fetch; in vitest it is a module TRANSFORM of react-markdown + highlight.js +
// KaTeX, which under a loaded worker can take longer than findBy's default
// timeout. Warming it once here means these tests assert on rendering, not on
// how busy the machine is — the same call the transcript makes on mount.
beforeAll(() => prefetchMarkdown())

describe('WebResult', () => {
  // `<Markdown>` defers its renderer (#1411) — await the rendered form.
  it('shows the fetched URL and renders the body as markdown', async () => {
    render(
      <WebResult
        name="WebFetch"
        input={{ url: 'https://example.com/docs' }}
        payload={{ content: 'See the **guide**.' }}
      />,
    )
    expect(screen.getByText('https://example.com/docs')).toBeInTheDocument()
    expect((await screen.findByText('guide')).tagName).toBe('STRONG')
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
