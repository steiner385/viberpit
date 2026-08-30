import { render, screen } from '@testing-library/react'
import { FileContent } from '../FileContent'
import { GenericResult } from '../GenericResult'

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

  it('keeps the [image] label for an elided (empty-data) image block', () => {
    const elided = { type: 'image', source: { type: 'base64', media_type: 'image/png', data: '' } }
    const { container } = render(<FileContent payload={{ content: [elided] }} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('[image]')
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
