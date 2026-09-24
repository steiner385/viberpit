import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MermaidDiagram } from './MermaidDiagram'
// The renderer directly — see MarkdownDoc.test.tsx's note.
import { MarkdownDoc as Markdown } from './MarkdownDoc'

const mermaid = vi.hoisted(() => ({ initialize: vi.fn(), render: vi.fn() }))
vi.mock('mermaid', () => ({ default: mermaid }))

beforeEach(() => { mermaid.render.mockReset() })

describe('MermaidDiagram', () => {
  it('renders an isolated image with readable source and removes the temporary container', async () => {
    mermaid.render.mockResolvedValue({ svg: '<svg xmlns="http://www.w3.org/2000/svg"><text>Start</text></svg>' })
    render(<MermaidDiagram source="graph TD; A-->B" />)
    const image = await screen.findByRole('img', { name: 'Mermaid diagram' })
    expect(image.getAttribute('src')).toMatch(/^data:image\/svg\+xml/)
    expect(document.querySelector('svg')).toBeNull()
    expect(screen.getByText('graph TD; A-->B')).toBeInTheDocument()
    expect(screen.getByText('Diagram source').parentElement).not.toHaveAttribute('open')
    expect(mermaid.initialize).toHaveBeenCalledWith(expect.objectContaining({ securityLevel: 'strict', startOnLoad: false }))
    expect(mermaid.render.mock.calls[0][2].isConnected).toBe(false)
  })

  it('does not remount diagrams when the Markdown parent rerenders', async () => {
    mermaid.render.mockResolvedValue({ svg: '<svg />' })
    const source = '```mermaid\ngraph TD; A-->B\n```'
    const { rerender } = render(<Markdown>{source}</Markdown>)
    await screen.findByRole('img')
    rerender(<Markdown>{source}</Markdown>)
    await act(async () => {})
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('img')).toBeInTheDocument()
  })

  it('shows source on failure without leaking an error diagram into the page', async () => {
    mermaid.render.mockRejectedValue(new Error('parse error'))
    render(<MermaidDiagram source="broken diagram" />)
    expect(await screen.findByText(/Unable to render diagram/)).toBeInTheDocument()
    expect(screen.getByText('Diagram source').parentElement).toHaveAttribute('open')
    expect(screen.queryByRole('img')).toBeNull()
    expect(mermaid.render.mock.calls[0][2].isConnected).toBe(false)
  })

  it('ignores a stale result after source changes', async () => {
    let resolveFirst!: (result: { svg: string }) => void
    mermaid.render.mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
    mermaid.render.mockResolvedValueOnce({ svg: '<svg>new</svg>' })
    const { rerender } = render(<MermaidDiagram source="old" />)
    await waitFor(() => expect(mermaid.render).toHaveBeenCalledTimes(1))
    rerender(<MermaidDiagram source="new" />)
    const image = await screen.findByRole('img')
    await act(async () => { resolveFirst({ svg: '<svg>old</svg>' }) })
    expect(decodeURIComponent(image.getAttribute('src')!)).toContain('<svg>new</svg>')
  })

  it('cleans up a render that completes after unmount', async () => {
    let finish!: (result: { svg: string }) => void
    mermaid.render.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const { unmount } = render(<MermaidDiagram source="graph TD; A-->B" />)
    await waitFor(() => expect(mermaid.render).toHaveBeenCalledTimes(1))
    const container = mermaid.render.mock.calls[0][2] as HTMLElement
    unmount()
    await act(async () => { finish({ svg: '<svg />' }) })
    expect(container.isConnected).toBe(false)
  })
})
