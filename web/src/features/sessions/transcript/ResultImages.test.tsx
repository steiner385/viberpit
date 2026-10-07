import { fireEvent, render, screen } from '@testing-library/react'
import { FileContent } from './FileContent'
import { GenericResult } from './GenericResult'
import { ResultImages } from './ResultImages'

const png = { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'abc123' } }

describe('FileContent image rendering', () => {
  it('renders an image block as an <img> instead of the [image] label', () => {
    const { container } = render(<FileContent payload={{ content: [png] }} />)
    const img = container.querySelector('img')!
    expect(img.getAttribute('src')).toBe('data:image/png;base64,abc123')
    expect(container.textContent).not.toContain('[image]')
    // No empty text pane alongside a pure-image result.
    expect(container.querySelector('pre')).toBeNull()
  })

  it('renders text and image together when both blocks are present', () => {
    const { container } = render(
      <FileContent payload={{ content: [{ type: 'text', text: 'meta line' }, png] }} />,
    )
    expect(container.querySelector('pre')!.textContent).toContain('meta line')
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('explains an unavailable elided image block', () => {
    const elided = { type: 'image', source: { type: 'base64', media_type: 'image/png', data: '' } }
    const { container } = render(<FileContent payload={{ content: [elided] }} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('Image unavailable')
  })

  it('still renders plain text content unchanged', () => {
    const { container } = render(<FileContent payload={{ content: 'just text' }} />)
    expect(container.querySelector('pre')!.textContent).toContain('just text')
    expect(container.querySelector('img')).toBeNull()
  })
})

describe('GenericResult image rendering', () => {
  it('renders an image block from an MCP tool result (e.g. screenshot)', () => {
    const { container } = render(<GenericResult payload={{ content: [png] }} />)
    const img = container.querySelector('img')!
    expect(img.getAttribute('src')).toBe('data:image/png;base64,abc123')
    expect(container.textContent).not.toContain('[image]')
  })

  it('renders accompanying text alongside the image', () => {
    const { container } = render(
      <GenericResult payload={{ content: [{ type: 'text', text: 'took screenshot' }, png] }} />,
    )
    expect(container.textContent).toContain('took screenshot')
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('links each image to open full-size in a new tab', () => {
    render(<GenericResult payload={{ content: [png] }} />)
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe('data:image/png;base64,abc123')
    expect(link.getAttribute('target')).toBe('_blank')
  })
})


describe('ResultImages recovery and actions', () => {
  it('survives malformed content blocks and explains images with missing sources', () => {
    render(<FileContent payload={{ content: [null, 7, 'invalid', { type: 'image' }, { type: 'image', source: null }] }} />)
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getAllByText(/Image unavailable/)).toHaveLength(2)
  })

  it.each(['', '/tmp/secret.png', 'javascript:alert(1)', 'https://user:password@example.com/image.png', 'data:image/png;base64,%%%'])('does not fetch or link malformed image %s', (url) => {
    render(<ResultImages images={[{ url, name: 'Screenshot', mime: '' }]} />)
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText(/Image unavailable/)).toHaveTextContent('share_artifact')
  })

  it('replaces missing image with recovery guidance and resets for a new URL', () => {
    const { rerender } = render(<ResultImages images={[{ url: '/api/attachments/missing', name: 'Screenshot', mime: '' }]} />)
    fireEvent.error(screen.getByRole('img'))
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByText(/Image unavailable/)).toBeVisible()
    rerender(<ResultImages images={[{ url: '/api/attachments/new', name: 'Screenshot', mime: '' }]} />)
    expect(screen.getByRole('img')).toHaveAttribute('src', '/api/attachments/new')
  })

  it('provides open and download actions for stored attachments', () => {
    render(<ResultImages images={[{ url: '/api/attachments/abc', name: 'Screenshot', mime: '' }]} />)
    expect(screen.getByRole('link', { name: /Open full size/ })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: /Download image/ })).toHaveAttribute('download', 'Screenshot')
    expect(screen.getByRole('img')).toHaveClass('max-w-full')
  })
})
