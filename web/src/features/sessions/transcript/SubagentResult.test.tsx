import { beforeAll, describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SubagentResult } from './SubagentResult'
import { prefetchMarkdown } from './markdownChunk'


// The markdown renderer is loaded lazily (#1411). In the browser that is a chunk
// fetch; in vitest it is a module TRANSFORM of react-markdown + highlight.js +
// KaTeX, which under a loaded worker can take longer than findBy's default
// timeout. Warming it once here means these tests assert on rendering, not on
// how busy the machine is — the same call the transcript makes on mount.
beforeAll(() => prefetchMarkdown())

describe('SubagentResult', () => {
  // `<Markdown>` defers its renderer (#1411): the source text is on screen
  // synchronously, the rendered form one microtask later — hence findBy.
  it('renders the subagent final text as markdown (bold is emphasized, not literal)', async () => {
    render(<SubagentResult payload={{ content: 'The fix is **defense in depth**.' }} />)
    const strong = await screen.findByText('defense in depth')
    expect(strong.tagName).toBe('STRONG')
    // The markdown asterisks must not appear literally.
    expect(screen.queryByText(/\*\*defense/)).toBeNull()
  })

  it('joins a text-block-array final message', () => {
    render(<SubagentResult payload={{ content: [{ type: 'text', text: 'agent done' }] }} />)
    expect(screen.getByText('agent done')).toBeInTheDocument()
  })

  it('falls back to the generic renderer for structured (non-text) payloads', () => {
    render(<SubagentResult payload={{ summary: 'x', count: 2 }} />)
    expect(screen.getByText('summary')).toBeInTheDocument()
    expect(screen.getByText('count')).toBeInTheDocument()
  })
})
