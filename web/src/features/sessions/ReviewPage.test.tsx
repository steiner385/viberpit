/**
 * ArchivedReviewPage — the archived transcript as a Reader page (finish-line
 * row 6): one h1 named after the session, a Back link, Unarchive as the header
 * action, no composer, busy/empty states from the page primitives, and the
 * scroll-up paging that the old read-only view had.
 */
import { afterEach, describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ArchivedReviewPage } from './ReviewPage'
import * as hooks from '@/api/hooks'
import { fetchOlderTurns } from '@/api/transcript'
import { mutationResult, queryResult } from '@/test/reactQuery'
import { resetDocumentTitle } from '@/shell/documentTitle'

vi.mock('@/api/hooks', () => ({
  useSessionTranscript: vi.fn(),
  useUnarchiveSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useArchivedSessions: vi.fn(() => ({ data: undefined })),
}))
vi.mock('@/api/transcript', () => ({ fetchOlderTurns: vi.fn() }))

const ARCHIVED = [{
  id: 'arc-99', name: 'Implement issue 16', account: 'alice', cwd: '/tmp/x', repo: 'o/r',
  engine: 'claude', engineSessionId: 'c-1', permissionMode: 'default',
  createdAt: '2026-06-01T10:00:00Z', archivedAt: '2026-06-02T10:00:00Z',
}]

function renderPage(sessionId = 'arc-99') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/sessions/archived/${sessionId}`]}>
        <Routes>
          <Route path="/sessions/archived/:archivedId" element={<ArchivedReviewPage sessionId={sessionId} />} />
          <Route path="/sessions/archived" element={<div data-testid="archived-list" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const TURNS = {
  events: [
    { type: 'user', turnId: 't1', text: 'Hello world' },
    { type: 'turnStart', turnId: 't2' },
    { type: 'textDelta', turnId: 't2', text: 'Hi there!' },
    { type: 'turnEnd', turnId: 't2' },
  ],
  earliestTurn: 0,
  totalTurns: 2,
}

// The virtualizer arms a 150 ms "scrolling reset" timer on every scroll
// (`isScrollingResetDelay`); a scroll fired by the last test in this file would
// let that timer fire after vitest has torn down jsdom, and react-virtual's
// notify then dispatches into React with no `window` ("Unhandled Error:
// ReferenceError: window is not defined" — the CI flake first seen on
// finish-line row 6). Let it fire while the environment still exists.
afterEach(() => new Promise<void>((resolve) => setTimeout(resolve, 200)))

describe('ArchivedReviewPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetDocumentTitle()
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult(ARCHIVED) as never)
  })

  it('is a Reader page: h1 = session name, document.title, variant, Back link', async () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage()
    expect(screen.getByRole('heading', { level: 1, name: 'Implement issue 16' })).toBeInTheDocument()
    await waitFor(() => expect(document.title).toBe('Implement issue 16 — ViberPit'))
    expect(document.querySelector('[data-page-variant="reader"]')).not.toBeNull()
    expect(screen.getByRole('link', { name: 'Back to archived sessions' })).toHaveAttribute('href', '/sessions/archived')
    expect(screen.getByText(/archived .* · read-only/i)).toBeInTheDocument()
  })

  it('falls back to the short id while the archived list has not arrived', () => {
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult(undefined) as never)
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage('arc-99abcdef')
    expect(screen.getByRole('heading', { level: 1, name: 'arc-99ab' })).toBeInTheDocument()
  })

  it('omits the archive date when the server sent none (no "Invalid Date" in the meta line)', () => {
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(
      queryResult([{ ...ARCHIVED[0], archivedAt: null as unknown as string }]) as never,
    )
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage()
    expect(screen.getByText(/^Archived · read-only · o\/r$/)).toBeInTheDocument()
    expect(screen.queryByText(/Invalid Date|1970/)).toBeNull()
  })

  it('shows the busy state while the transcript is fetching', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({ data: undefined, isLoading: true } as never)
    renderPage()
    expect(screen.getByTestId('page-content')).toHaveAttribute('aria-busy', 'true')
    expect(screen.queryByTestId('transcript-scroll')).toBeNull()
  })

  it('shows the empty state when the events array is empty', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage()
    expect(screen.getByText(/no transcript available/i)).toBeInTheDocument()
  })

  it('renders turns from transcript events without a composer', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult(TURNS) as never)
    renderPage()
    expect(screen.getByText('Hello world')).toBeInTheDocument()
    expect(screen.getByText('Hi there!')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('calls useSessionTranscript with enabled=true and a bounded initial window', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({ data: undefined, isLoading: true } as never)
    renderPage()
    expect(vi.mocked(hooks.useSessionTranscript)).toHaveBeenCalledWith('arc-99', true, 100)
  })

  it('pages in older turns when scrolled to the top', async () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ ...TURNS, earliestTurn: 10, totalTurns: 30 }) as never)
    vi.mocked(fetchOlderTurns).mockResolvedValue({ events: [], earliestTurn: 0, totalTurns: 30 })
    renderPage()
    const scroller = screen.getByTestId('transcript-scroll')
    // `writable: true` (not just `configurable`): TranscriptView itself writes
    // scrollTop (its prepend-anchor and pin-to-bottom effects), and a
    // read-only stub throws the moment either of those runs.
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    // Mounting the virtualizer routes its first row measurement through
    // TranscriptView's programmatic-scroll guard, which flags the mount frame;
    // a simulated user scroll has to land in a later frame, as a real one would
    // (ported from the old read-only view's test's scroll-trigger mechanics).
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
    fireEvent.scroll(scroller)
    await waitFor(() => expect(fetchOlderTurns).toHaveBeenCalledWith('arc-99', 10))
  })

  it('does not page when already at the session start', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult(TURNS) as never)
    renderPage()
    const scroller = screen.getByTestId('transcript-scroll')
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    fireEvent.scroll(scroller)
    expect(fetchOlderTurns).not.toHaveBeenCalled()
  })

  it('Unarchive is the header action and navigates to the archived list on success', async () => {
    const user = userEvent.setup()
    const mutate = vi.fn((_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.())
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue(mutationResult({ mutate } as never) as never)
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage()
    await user.click(screen.getByRole('button', { name: /restore this session from archive/i }))
    expect(mutate).toHaveBeenCalledWith('arc-99', expect.objectContaining({ onSuccess: expect.any(Function) }))
    expect(await screen.findByTestId('archived-list')).toBeInTheDocument()
  })

  it('shows "Restoring…" and disables the button while unarchive is pending', () => {
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue(mutationResult({ mutate: vi.fn(), isPending: true }) as never)
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage()
    const btn = screen.getByRole('button', { name: /restore this session from archive/i })
    expect(btn).toBeDisabled()
    expect(btn).toHaveTextContent('Restoring…')
  })
})
