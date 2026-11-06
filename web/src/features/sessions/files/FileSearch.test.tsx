/**
 * The Files tab's content search (D-20): Contents switches the list column to a
 * debounced `GET /sessions/{id}/files/search`, results grouped by file with the
 * match marked, and a result opens the viewer at its line (`?file=&line=`).
 * The line text is file content: it must render as text, never as HTML.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import FilesTab from './FilesTab'
import { useFileTarget } from '../sessionView'
import type { SessionFileSearch } from './types'

const HITS: SessionFileSearch = {
  matches: [
    { path: 'src/a.ts', line: 3, text: 'const needle = 1', start: 6, end: 12 },
    { path: 'src/a.ts', line: 9, text: 'return needle', start: 7, end: 13 },
    { path: 'docs/b.md', line: 12, text: 'a needle here', start: 2, end: 8 },
  ],
  truncated: false,
  timedOut: false,
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

let fetchSpy: { mock: { calls: unknown[][] } }
function serve(search: (u: URL) => Response = () => json(200, HITS)) {
  fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(async (input) => {
    const url = new URL(String(input), 'http://x')
    if (url.pathname === '/api/sessions/s1/files') return json(200, { files: [{ path: 'src/a.ts' }], truncated: false })
    if (url.pathname === '/api/sessions/s1/files/search') return search(url)
    if (url.pathname === '/api/sessions/s1/files/content') {
      const path = url.searchParams.get('path')!
      return json(200, { path, text: 'x\n'.repeat(20), binary: false, truncated: false, size: 40 })
    }
    return json(404, {})
  })
}

const searches = () => fetchSpy.mock.calls
  .map(([input]): URL => new URL(String(input), 'http://x'))
  .filter((u) => u.pathname === '/api/sessions/s1/files/search')

function Mounted() {
  const [target, setFile] = useFileTarget()
  return <FilesTab sessionId="s1" target={target} onOpen={setFile} />
}

function LocationProbe() {
  return <output data-testid="location-search">{useLocation().search}</output>
}
const search = () => new URLSearchParams(screen.getByTestId('location-search').textContent ?? '')

async function openContents() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/sessions/s1?view=files']}>
        <Mounted />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'Contents' }))
  const box = await screen.findByRole('combobox', { name: 'Search file contents' })
  return { user, box, qc }
}

beforeEach(() => {
  vi.restoreAllMocks()
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: vi.fn() })
})
afterEach(() => {
  delete (HTMLElement.prototype as { scrollTo?: unknown }).scrollTo
})

describe('FileSearch', () => {
  it('stays idle below two characters and sends nothing', async () => {
    serve()
    const { user, box } = await openContents()
    expect(screen.getByText(/Type at least 2 characters/)).toBeInTheDocument()
    await user.type(box, 'n')
    await new Promise((r) => setTimeout(r, 400))
    expect(searches()).toEqual([])
    expect(screen.getByText(/Type at least 2 characters/)).toBeInTheDocument()
  })

  it('debounces typing into one request for the final query', async () => {
    serve()
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    expect(await screen.findByRole('listbox', { name: 'Search results' })).toBeInTheDocument()
    expect(searches().map((u) => u.searchParams.get('q'))).toEqual(['needle'])
    expect(searches()[0].searchParams.get('regex')).toBeNull()
  })

  it('shows a loading state while the search is in flight', async () => {
    let release!: () => void
    serve(() => new Promise<Response>((r) => { release = () => r(json(200, HITS)) }) as unknown as Response)
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    expect(await screen.findByText('Searching…')).toBeInTheDocument()
    release()
    expect(await screen.findByRole('listbox', { name: 'Search results' })).toBeInTheDocument()
  })

  it('groups results by file with the line number and the match marked', async () => {
    serve()
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    const a = await screen.findByRole('group', { name: 'src/a.ts' })
    const rows = within(a).getAllByRole('option')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('3const needle = 1')
    expect(rows[0].querySelector('mark')?.textContent).toBe('needle')
    expect(within(screen.getByRole('group', { name: 'docs/b.md' })).getAllByRole('option')).toHaveLength(1)
  })

  it('renders a match as text, never as HTML', async () => {
    const payload = '<img src=x onerror="window.__pwned=1"> needle'
    serve(() => json(200, {
      matches: [{ path: 'evil.html', line: 1, text: payload, start: 0, end: 10 }],
      truncated: false, timedOut: false,
    }))
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    const row = await screen.findByRole('option')
    expect(row).toHaveTextContent(payload)
    expect(row.querySelector('mark')?.textContent).toBe('<img src=x')
    expect(document.querySelector('img')).toBeNull()
    expect((window as { __pwned?: number }).__pwned).toBeUndefined()
  })

  it('slices the mark by code points, the server offsets', async () => {
    serve(() => json(200, {
      matches: [{ path: 'e.txt', line: 1, text: '😀 needle', start: 2, end: 8 }],
      truncated: false, timedOut: false,
    }))
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    const row = await screen.findByRole('option')
    expect(row.querySelector('mark')?.textContent).toBe('needle')
  })

  it('a click opens the viewer at the result line', async () => {
    serve()
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    const b = await screen.findByRole('group', { name: 'docs/b.md' })
    await user.click(within(b).getByRole('option'))
    await waitFor(() => expect(search().get('file')).toBe('docs/b.md'))
    expect(search().get('line')).toBe('12')
    expect(search().get('view')).toBe('files')
  })

  it('Enter opens the first result', async () => {
    serve()
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    const options = await screen.findAllByRole('option')
    expect(box).toHaveAttribute('aria-activedescendant', options[0].id)
    expect(options[0]).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('{Enter}')
    await waitFor(() => expect(search().get('line')).toBe('3'))
    expect(search().get('file')).toBe('src/a.ts')
  })

  it('arrows move the active result, wrapping, and Enter opens it', async () => {
    serve()
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    const options = await screen.findAllByRole('option')
    await user.keyboard('{ArrowDown}{ArrowDown}')
    expect(box).toHaveAttribute('aria-activedescendant', options[2].id)
    expect(options[2]).toHaveAttribute('aria-selected', 'true')
    expect(options[0]).toHaveAttribute('aria-selected', 'false')
    await user.keyboard('{ArrowDown}')
    expect(box).toHaveAttribute('aria-activedescendant', options[0].id)
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveAttribute('aria-activedescendant', options[2].id)
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveAttribute('aria-activedescendant', options[1].id)
    expect(box).toHaveFocus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(search().get('line')).toBe('9'))
    expect(search().get('file')).toBe('src/a.ts')
  })

  it('Enter waits while the rows answer an older query', async () => {
    serve()
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    await screen.findAllByRole('option')
    await user.type(box, 'x{Enter}')
    expect(search().get('file')).toBeNull()
  })

  it('the Regex toggle sends regex=true and marks nothing for an empty span', async () => {
    serve((u) => json(200, u.searchParams.get('regex') === 'true'
      ? { matches: [{ path: 'r.ts', line: 4, text: 'foo bar', start: 0, end: 0 }], truncated: false, timedOut: false }
      : HITS))
    const { user, box } = await openContents()
    await user.type(box, 'fo+')
    await screen.findAllByRole('option')
    const toggle = screen.getByRole('button', { name: 'Regex' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    const row = await screen.findByRole('option', { name: /foo bar/ })
    expect(row.querySelector('mark')).toBeNull()
    const last = searches().at(-1)!
    expect(last.searchParams.get('regex')).toBe('true')
    expect(last.searchParams.get('q')).toBe('fo+')
  })

  it('says No matches for an empty result', async () => {
    serve(() => json(200, { matches: [], truncated: false, timedOut: false }))
    const { user, box } = await openContents()
    await user.type(box, 'zzz')
    expect(await screen.findByText('No matches')).toBeInTheDocument()
  })

  it('says when the result was cut at the cap', async () => {
    serve(() => json(200, { ...HITS, truncated: true }))
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    expect(await screen.findByText('Showing first 3 matches')).toBeInTheDocument()
  })

  it('says when the search timed out with partial results', async () => {
    serve(() => json(200, { ...HITS, truncated: true, timedOut: true }))
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    expect(await screen.findByText('Search timed out — showing partial results')).toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(3)
    expect(screen.queryByText(/Showing first/)).toBeNull()
  })

  it.each([
    [400, { detail: { code: 'BAD_REGEX' } }, 'Invalid regular expression'],
    [400, { detail: { code: 'BAD_QUERY' } }, "This search can't be run."],
    [404, { detail: { code: 'FILE_NOT_FOUND' } }, 'This session has no worktree to search.'],
    [409, { detail: { code: 'daemon_outdated', message: 'Update the daemon.' } }, 'Update the daemon.'],
  ])('a %i %j says so inline', async (status, body, text) => {
    serve(() => json(status, body))
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    expect(await screen.findByText(text)).toBeInTheDocument()
    expect(screen.queryByRole('option')).toBeNull()
  })

  it('a SEARCH_FAILED offers a retry', async () => {
    let fail = true
    serve(() => (fail ? json(500, { detail: { code: 'SEARCH_FAILED' } }) : json(200, HITS)))
    const { user, box } = await openContents()
    await user.type(box, 'needle')
    expect(await screen.findByText(/Couldn.t search the files/)).toBeInTheDocument()
    fail = false
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findAllByRole('option')).toHaveLength(3)
  })

  it('a failed refetch hides the old rows: nothing to pick, nothing active', async () => {
    let fail = false
    serve(() => (fail ? json(500, { detail: { code: 'SEARCH_FAILED' } }) : json(200, HITS)))
    const { user, box, qc } = await openContents()
    await user.type(box, 'needle')
    expect(await screen.findAllByRole('option')).toHaveLength(3)
    fail = true
    await qc.refetchQueries()
    expect(await screen.findByText(/Couldn.t search the files/)).toBeInTheDocument()
    expect(screen.queryByRole('option')).toBeNull()
    expect(box).toHaveAttribute('aria-expanded', 'false')
    expect(box).not.toHaveAttribute('aria-activedescendant')
    await user.keyboard('{Enter}')
    expect(search().get('file')).toBeNull()
  })

  it('a single emoji is one character: below the minimum, nothing is sent', async () => {
    serve()
    const { user, box } = await openContents()
    await user.click(box)
    await user.paste('😀')
    expect(box).toHaveValue('😀')
    await new Promise((r) => setTimeout(r, 400))
    expect(searches()).toEqual([])
    expect(screen.getByText(/Type at least 2 characters/)).toBeInTheDocument()
  })

  it('caps the query at the server limit of 64 characters, counted in code points', async () => {
    serve()
    const { user, box } = await openContents()
    await user.click(box)
    await user.paste('😀'.repeat(70))
    expect(box).toHaveValue('😀'.repeat(64))
    await waitFor(() => expect(searches()).toHaveLength(1))
    expect(searches()[0].searchParams.get('q')).toBe('😀'.repeat(64))
  })

  it('carries the name query over, and Contents goes back to the file list', async () => {
    serve()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/sessions/s1?view=files']}>
          <Mounted />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const user = userEvent.setup()
    await user.type(screen.getByRole('searchbox', { name: 'Search files' }), 'needle')
    await user.click(screen.getByRole('button', { name: 'Contents' }))
    expect(await screen.findByRole('combobox', { name: 'Search file contents' })).toHaveValue('needle')
    expect(await screen.findAllByRole('option')).toHaveLength(3)
    await user.click(screen.getByRole('button', { name: 'Contents' }))
    expect(screen.getByRole('searchbox', { name: 'Search files' })).toBeInTheDocument()
  })
})
