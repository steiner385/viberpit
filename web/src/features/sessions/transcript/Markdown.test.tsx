import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Markdown } from './Markdown'
import { prefetchMarkdown } from './markdownChunk'

// The lazy BOUNDARY (#1411), not the renderer — `MarkdownDoc.test.tsx` owns the
// plugin chain. What matters here is that deferring ~240kB of renderer never
// costs the reader the text itself.
//
// Deliberately NOT warmed with `prefetchMarkdown()` in a `beforeAll`, unlike the
// consumer suites: the cold path IS the thing under test here.
describe('Markdown (lazy boundary)', () => {
  it('shows the source text immediately, before the renderer chunk lands', () => {
    render(<Markdown>{'See the **guide**.'}</Markdown>)
    // Synchronously, on the very first frame: no spinner, no empty box.
    expect(screen.getByText('See the **guide**.')).toBeInTheDocument()
  })

  it('upgrades to rendered markdown once the chunk resolves', async () => {
    render(<Markdown>{'See the **guide**.'}</Markdown>)
    expect((await screen.findByText('guide')).tagName).toBe('STRONG')
  })

  it('passes `streaming` through to the renderer', async () => {
    // A streaming mermaid fence stays readable as code rather than rendering.
    await prefetchMarkdown()
    render(<Markdown streaming>{'```mermaid\ngraph TD; A-->'}</Markdown>)
    expect(await screen.findByText(/graph TD/)).toBeInTheDocument()
    expect(screen.queryByRole('figure')).toBeNull()
  })

  it('prefetchMarkdown never rejects, even called twice', async () => {
    await expect(prefetchMarkdown()).resolves.toBeUndefined()
    await expect(prefetchMarkdown()).resolves.toBeUndefined()
  })
})
