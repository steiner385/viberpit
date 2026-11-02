/**
 * The session's Files tab (D-7): a search box over `GET /sessions/{id}/files`
 * and the read-only viewer. The open file and line live in the URL
 * (`?view=files&file=<path>&line=<n>`, `sessionView.ts`), so a transcript link
 * or a reload lands on the same place.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import FilesTab from './FilesTab'
import { useFileTarget } from '../sessionView'

const LIST = {
  files: [{ path: 'web/src/Composer.tsx' }, { path: 'web/src/compose/util.ts' }],
  truncated: false,
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

let fetchSpy: { mock: { calls: unknown[][] } }
function serve({ list = LIST, listStatus = 200 }: { list?: unknown; listStatus?: number } = {}) {
  fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(async (input) => {
    const url = new URL(String(input), 'http://x')
    if (url.pathname === '/api/sessions/s1/files') return json(listStatus, list)
    if (url.pathname === '/api/sessions/s1/files/content') {
      const path = url.searchParams.get('path')!
      return json(200, { path, text: `// ${path}\nexport {}\n`, binary: false, truncated: false, size: 20 })
    }
    return json(404, {})
  })
}

const listQueries = () => fetchSpy.mock.calls
  .map(([input]): URL => new URL(String(input), 'http://x'))
  .filter((u) => u.pathname === '/api/sessions/s1/files')
  .map((u): string | null => u.searchParams.get('q'))

/** The tab as `SessionBody` mounts it: the URL state from `useFileTarget`. */
function Mounted() {
  const [target, setFile] = useFileTarget()
  return <FilesTab sessionId="s1" target={target} onOpen={setFile} />
}

function LocationProbe() {
  return <output data-testid="location-search">{useLocation().search}</output>
}
const search = () => new URLSearchParams(screen.getByTestId('location-search').textContent ?? '')

function renderTab(path = '/sessions/s1?view=files') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Mounted />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: vi.fn() })
})
afterEach(() => {
  delete (HTMLElement.prototype as { scrollTo?: unknown }).scrollTo
})

describe('FilesTab', () => {
  it('lists the recent files before anything is typed', async () => {
    serve()
    renderTab()
    expect(await screen.findByRole('button', { name: /Composer\.tsx/ })).toBeInTheDocument()
    expect(listQueries()).toEqual([''])
  })

  it('the search box queries the file list and lists the matches', async () => {
    serve()
    renderTab()
    await screen.findByRole('button', { name: /Composer\.tsx/ })
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search files' }), 'comp')
    await waitFor(() => expect(listQueries()).toContain('comp'))
    // Debounced: one query for the word, not one per keystroke.
    expect(listQueries().filter((q) => q !== '' && q !== 'comp')).toEqual([])
    expect(screen.getByRole('button', { name: /util\.ts/ })).toBeInTheDocument()
  })

  it('clicking a file opens it in the viewer and puts it in the URL', async () => {
    serve()
    renderTab()
    await userEvent.click(await screen.findByRole('button', { name: /Composer\.tsx/ }))
    expect(search().get('view')).toBe('files')
    expect(search().get('file')).toBe('web/src/Composer.tsx')
    expect(search().get('line')).toBeNull()
    expect(await screen.findByText('// web/src/Composer.tsx')).toBeInTheDocument()
  })

  it('?file=&line= opens that file at that line', async () => {
    serve()
    renderTab('/sessions/s1?view=files&file=web%2Fsrc%2Fcompose%2Futil.ts&line=2')
    await waitFor(() => expect(document.querySelector('[data-line="2"]')).toHaveAttribute('data-target'))
    expect(screen.getByText('// web/src/compose/util.ts')).toBeInTheDocument()
  })

  it("All files (the narrow layout's way back) closes the file, staying on the tab", async () => {
    serve()
    renderTab('/sessions/s1?view=files&file=web%2Fsrc%2FComposer.tsx&line=2')
    expect(await screen.findByText('// web/src/Composer.tsx')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'All files' }))
    expect(search().get('file')).toBeNull()
    expect(search().get('line')).toBeNull()
    expect(search().get('view')).toBe('files')
    expect(screen.queryByText('// web/src/Composer.tsx')).toBeNull()
  })

  it('a truncated list says only part of the worktree was searched', async () => {
    serve({ list: { ...LIST, truncated: true } })
    renderTab()
    expect(await screen.findByText(/only the first 20,000 files are searched/i)).toBeInTheDocument()
  })

  it('no match says so', async () => {
    serve({ list: { files: [], truncated: false } })
    renderTab()
    expect(await screen.findByText(/no files/i)).toBeInTheDocument()
  })

  it('a list error offers a retry', async () => {
    serve({ listStatus: 500, list: { detail: 'boom' } })
    renderTab()
    expect(await screen.findByText(/couldn.t list the files/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})
