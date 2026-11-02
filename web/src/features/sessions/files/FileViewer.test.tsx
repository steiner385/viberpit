/**
 * The Files tab's read-only viewer (D-7): one worktree file from
 * `GET /sessions/{id}/files/content`, line-numbered and highlighted, windowed so
 * a 512 KiB file does not mount every line. `line` scrolls to, highlights and
 * focuses that line; binary, truncated and refused files say so.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import FileViewer from './FileViewer'

function serve(status: number, body: unknown) {
  return vi.spyOn(global, 'fetch').mockImplementation(async () => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  }))
}

function file(text: string | null, over: Record<string, unknown> = {}) {
  return { path: 'src/a.ts', text, binary: false, truncated: false, size: (text ?? '').length, ...over }
}

const HUNDRED = Array.from({ length: 100 }, (_, i) => `const line${i + 1} = ${i + 1}`).join('\n') + '\n'

function renderViewer(props: { path?: string; line?: number | null } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <FileViewer sessionId="s1" path={props.path ?? 'src/a.ts'} line={props.line ?? null} />
    </QueryClientProvider>,
  )
}

const scrollTo = vi.fn()
beforeEach(() => {
  vi.restoreAllMocks()
  scrollTo.mockReset()
  // jsdom has no Element.scrollTo; the virtualizer's scrollToIndex goes through it.
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: scrollTo })
})
afterEach(() => {
  delete (HTMLElement.prototype as { scrollTo?: unknown }).scrollTo
})

describe('FileViewer', () => {
  it('reads the file by its worktree path and draws numbered lines', async () => {
    const fetchSpy = serve(200, file('import { x } from "y"\nexport default x\n'))
    renderViewer()
    expect(await screen.findByText('export default x')).toBeInTheDocument()
    expect(fetchSpy).toHaveBeenCalledWith('/api/sessions/s1/files/content?path=src%2Fa.ts', expect.anything())
    const row = document.querySelector('[data-line="2"]')
    expect(row).not.toBeNull()
    expect(row!.querySelector('[data-line-no]')).toHaveTextContent('2')
    // Room for every digit of the last line number, plus the cell's padding (border-box).
    expect(row!.querySelector('[data-line-no]')!.getAttribute('style')).toContain('calc(1ch + 0.5rem)')
    // A trailing newline ends the last line; it is not an extra empty one.
    expect(document.querySelector('[data-line="3"]')).toBeNull()
  })

  it('highlights by extension once the shared highlighter chunk loads', async () => {
    serve(200, file('export const a = 1\n'))
    renderViewer()
    await waitFor(() => expect(document.querySelector('[data-line="1"] .hljs-keyword')).not.toBeNull())
  })

  it('a line over 5,000 chars renders plain (never highlighted) and does not widen the frame past 5,000 columns', async () => {
    const long = `const big = "${'a'.repeat(6000)}"`
    serve(200, file(`export const a = 1\n${long}\n`))
    renderViewer()
    // Line 1 still highlights, so the highlighter has loaded by the time we look at line 2.
    await waitFor(() => expect(document.querySelector('[data-line="1"] .hljs-keyword')).not.toBeNull())
    const row = document.querySelector('[data-line="2"]')!
    expect(row).toHaveTextContent(long)
    expect(row.querySelector('[class^="hljs-"]')).toBeNull()
    // The inner box is sized for the clamped width: 1 digit + 5,000 columns.
    const inner = row.parentElement as HTMLElement
    expect(inner.style.width).toBe('calc(5001ch + 1rem)')
  })

  it('windows a large file instead of mounting every line', async () => {
    serve(200, file(Array.from({ length: 5000 }, (_, i) => `x${i}`).join('\n')))
    renderViewer()
    await screen.findByText('x0')
    expect(document.querySelectorAll('[data-line]').length).toBeLessThan(200)
  })

  it('line=40 scrolls to, highlights and focuses line 40', async () => {
    // jsdom does no layout: give the frame a 600px viewport over 100 rows of 20px,
    // or the virtualizer clamps every scroll offset to 0.
    for (const [dim, px] of [['scrollHeight', 2000], ['clientHeight', 600]] as const) {
      vi.spyOn(HTMLElement.prototype, dim, 'get').mockReturnValue(px)
    }
    serve(200, file(HUNDRED))
    renderViewer({ line: 40 })
    const target = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-line="40"]')
      expect(el).not.toBeNull()
      return el!
    })
    expect(target).toHaveAttribute('data-target')
    expect(target).toHaveTextContent('const line40 = 40')
    // No other line carries the highlight.
    expect(document.querySelectorAll('[data-target]')).toHaveLength(1)
    await waitFor(() => expect(target).toHaveFocus())
    // Centred: row 40 starts at 780px; minus half the 600px viewport, plus half a row.
    await waitFor(() => expect(scrollTo).toHaveBeenLastCalledWith(expect.objectContaining({ top: 490 })))
  })

  it('a line past the end jumps to the last line', async () => {
    serve(200, file('a\nb\nc\n'))
    renderViewer({ line: 999 })
    await waitFor(() => expect(document.querySelector('[data-line="3"]')).toHaveAttribute('data-target'))
  })

  it('a binary file says so and draws no lines', async () => {
    serve(200, file(null, { path: 'logo.png', binary: true, size: 2048 }))
    renderViewer({ path: 'logo.png' })
    expect(await screen.findByText('Binary file — not shown')).toBeInTheDocument()
    expect(document.querySelector('[data-line]')).toBeNull()
  })

  it('a truncated file shows the notice above what was read', async () => {
    serve(200, file('first\n', { truncated: true, size: 2 * 1024 * 1024 }))
    renderViewer()
    expect(await screen.findByText(/showing the first 512 KiB of 2,048 KiB/i)).toBeInTheDocument()
    expect(screen.getByText('first')).toBeInTheDocument()
  })

  it("a 400 (a path outside the worktree) says Can't open this path", async () => {
    serve(400, { detail: { code: 'PATH_OUTSIDE_WORKTREE' } })
    renderViewer({ path: '../etc/passwd' })
    expect(await screen.findByText("Can't open this path")).toBeInTheDocument()
  })

  it('a 404 says the file is not there', async () => {
    serve(404, { detail: { code: 'FILE_NOT_FOUND' } })
    renderViewer()
    expect(await screen.findByText(/file not found/i)).toBeInTheDocument()
  })

  it("a 409 daemon_outdated shows the server's explanation", async () => {
    serve(409, { detail: {
      code: 'daemon_outdated',
      message: 'The session daemon has not been updated to read session files yet; try again after the next rollout.',
    } })
    renderViewer()
    expect(await screen.findByText(/session daemon has not been updated/i)).toBeInTheDocument()
  })
})
