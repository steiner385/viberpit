import { beforeAll, describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ToolResult } from './ToolResult'
import { prefetchMarkdown } from './markdownChunk'


// The markdown renderer is loaded lazily (#1411). In the browser that is a chunk
// fetch; in vitest it is a module TRANSFORM of react-markdown + highlight.js +
// KaTeX, which under a loaded worker can take longer than findBy's default
// timeout. Warming it once here means these tests assert on rendering, not on
// how busy the machine is — the same call the transcript makes on mount.
beforeAll(() => prefetchMarkdown())

describe('ToolResult', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('dispatches edit → DiffRenderer, reconstructing the diff from input (not the result)', () => {
    const { container } = render(
      <ToolResult
        kind="edit"
        ok={true}
        input={{ old_string: 'old', new_string: 'new' }}
        payload={{ content: 'The file has been updated.' }}
      />,
    )
    const rows = Array.from(container.querySelectorAll('[data-diff-row]'))
    expect(rows.map((r) => [r.getAttribute('data-kind'), r.querySelector('[data-sign]')?.textContent, r.textContent])).toEqual([
      ['del', '-', '-old'],
      ['add', '+', '+new'],
    ])
    // The result success message must NOT be rendered as a diff line.
    expect(screen.queryByText(/has been updated/)).toBeNull()
  })

  it('dispatches write → DiffRenderer', () => {
    const { container } = render(<ToolResult kind="write" ok={true} input={{ content: 'written' }} payload={{ content: 'ok' }} />)
    expect(container.querySelector('[data-diff-row][data-kind="add"]')).toHaveTextContent('written')
  })

  it('caps a long edit at 200 lines and "show all" reveals the rest', async () => {
    const user = userEvent.setup()
    const content = Array.from({ length: 300 }, (_, i) => `line${i}`).join('\n')
    const { container } = render(<ToolResult kind="write" ok={true} input={{ content }} payload={{}} />)
    expect(container.querySelectorAll('[data-diff-row]')).toHaveLength(200)
    await user.click(screen.getByRole('button', { name: /show all 300 lines/i }))
    expect(container.querySelectorAll('[data-diff-row]')).toHaveLength(300)
    expect(screen.queryByRole('button', { name: /show all/i })).toBeNull()
  })

  it('has no error border on a diff that applied', () => {
    const { container } = render(<ToolResult kind="edit" ok={true} input={{ old_string: 'a', new_string: 'b' }} payload={{}} />)
    expect(container.firstChild).not.toHaveClass('border-danger')
  })

  it('dispatches read → FileContent', () => {
    render(<ToolResult kind="read" ok={true} input={{}} payload={{ content: 'file data' }} />)
    expect(screen.getByText(/file data/)).toBeInTheDocument()
  })

  it('dispatches bash → BashResult', () => {
    render(<ToolResult kind="bash" ok={true} input={{}} payload={{ content: 'bash output' }} />)
    expect(screen.getByText(/bash output/)).toBeInTheDocument()
  })

  it('dispatches todo → TodoList', () => {
    render(<ToolResult kind="todo" ok={true} input={{}} payload={{ todos: [{ content: 'task', status: 'pending' }] }} />)
    expect(screen.getByText('task')).toBeInTheDocument()
  })

  it('dispatches search → SearchResult (lines + count, no envelope)', () => {
    render(<ToolResult kind="search" ok={true} name="Grep" input={{}} payload={{ content: 'a.ts:1: x\nb.ts:2: x' }} />)
    expect(screen.getByText('2 matches')).toBeInTheDocument()
    expect(screen.queryByText(/"content"/)).toBeNull()
  })

  // `<Markdown>` defers its renderer (#1411) — await the rendered form.
  it('dispatches task → SubagentResult (markdown, no envelope)', async () => {
    render(<ToolResult kind="task" ok={true} input={{}} payload={{ content: 'done **well**' }} />)
    expect((await screen.findByText('well')).tagName).toBe('STRONG')
  })

  it('dispatches web → WebResult (shows url, no envelope)', () => {
    render(<ToolResult kind="web" ok={true} name="WebFetch" input={{ url: 'https://x.io' }} payload={{ content: 'body' }} />)
    expect(screen.getByText('https://x.io')).toBeInTheDocument()
    expect(screen.queryByText(/"content"/)).toBeNull()
  })

  it('dispatches mcp → GenericResult, extracting text not the envelope', () => {
    render(<ToolResult kind="mcp" ok={true} name="mcp__github__list_prs" input={{}} payload={{ content: 'hello' }} />)
    expect(screen.getByText('hello')).toBeInTheDocument()
    expect(screen.queryByText(/"content"/)).toBeNull()
  })

  it('renders an unknown kind structurally, never as the raw envelope', () => {
    render(<ToolResult kind="unknown" ok={true} input={{}} payload={{ foo: 'bar' }} />)
    expect(screen.getByText('foo')).toBeInTheDocument()
    expect(screen.getByText('bar')).toBeInTheDocument()
  })

  it('styles text results as an error when ok is false', () => {
    const { container } = render(<ToolResult kind="unknown" ok={false} input={{}} payload={{ content: 'boom' }} />)
    expect(container.querySelector('pre')).toHaveClass('bg-danger-wash')
  })

  it('passes ok through to read → FileContent', () => {
    const { container } = render(<ToolResult kind="read" ok={false} input={{}} payload={{ content: 'denied' }} />)
    expect(container.querySelector('pre')).toHaveClass('bg-danger-wash')
  })

  it('passes ok through to edit → DiffRenderer (a failed edit gets a red border)', () => {
    const { container } = render(
      <ToolResult kind="edit" ok={false} input={{ old_string: 'a', new_string: 'b' }} payload={{ content: 'failed' }} />,
    )
    expect(container.firstChild).toHaveClass('border-danger')
  })
})

describe('ToolResult: a Codex file change', () => {
  // The app-server reports WHICH files changed and how, never the text either
  // side, so there is no diff to render — list what the engine did tell us.
  const changes = [
    { path: 'server/app.py', kind: 'update' },
    { path: 'server/new.py', kind: 'add' },
    { path: 'server/old.py', kind: 'delete' },
  ]

  it('lists every changed path and what happened to it instead of an empty diff', () => {
    const { container } = render(<ToolResult kind="edit" ok={true} input={{ changes }} payload={{}} />)
    for (const t of ['server/app.py', 'server/new.py', 'server/old.py', 'updated', 'added', 'deleted']) {
      expect(screen.getByText(t)).toBeInTheDocument()
    }
    expect(container.querySelector('[data-diff-row]')).toBeNull()
  })

  it('flags a failed change list in red', () => {
    const { container } = render(<ToolResult kind="edit" ok={false} input={{ changes }} payload={{}} />)
    expect(container.firstChild).toHaveClass('border-danger')
  })

  it('renders nothing when there is neither a diff nor a change list', () => {
    const { container } = render(<ToolResult kind="edit" ok={true} input={{}} payload={{}} />)
    expect(container.querySelector('ul')).toBeNull()
    expect(container.querySelector('[data-diff-row]')).toBeNull()
  })
})
