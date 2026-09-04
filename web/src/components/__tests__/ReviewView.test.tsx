/**
 * ReviewView.test.tsx
 *
 * Tests for the read-only transcript viewer for archived sessions:
 * - Renders turns from events payload read-only (no composer)
 * - Loading state while transcript is fetching
 * - Empty state when no transcript events exist
 * - Back button calls onBack
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ReviewView } from '../ReviewView'
import * as hooks from '@/api/hooks'
import { fetchOlderTurns } from '@/api/transcript'
import { queryResult } from '@/test/reactQuery'

vi.mock('@/api/hooks', () => ({
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSessionTranscript: vi.fn(),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
  useUnarchiveSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

vi.mock('@/api/transcript', () => ({
  fetchOlderTurns: vi.fn(),
}))

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('ReviewView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a loading state while transcript is fetching', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({
      data: undefined,
      isLoading: true,
    } as ReturnType<typeof hooks.useSessionTranscript>)

    render(<ReviewView sessionId="arc-1" onBack={vi.fn()} />, { wrapper })

    expect(screen.getByText(/loading transcript/i)).toBeInTheDocument()
  })

  it('shows empty state when events array is empty', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }))

    render(<ReviewView sessionId="arc-1" onBack={vi.fn()} />, { wrapper })

    expect(screen.getByText(/no transcript available/i)).toBeInTheDocument()
  })

  it('renders turns from transcript events without a composer', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({
      data: {
        events: [
          { type: 'user', turnId: 'turn-1', text: 'Hello world' },
          { type: 'turnStart', turnId: 'turn-2' },
          { type: 'textDelta', turnId: 'turn-2', text: 'Hi there!' },
          { type: 'turnEnd', turnId: 'turn-2' },
          { type: 'replayComplete' },
        ],
      },
      isLoading: false,
    } as ReturnType<typeof hooks.useSessionTranscript>)

    render(<ReviewView sessionId="arc-1" onBack={vi.fn()} />, { wrapper })

    expect(screen.getByText('Hello world')).toBeInTheDocument()
    expect(screen.getByText('Hi there!')).toBeInTheDocument()
    // No composer textarea should appear
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('shows "Read-only (archived)" header note', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }))

    render(<ReviewView sessionId="arc-1" onBack={vi.fn()} />, { wrapper })

    expect(screen.getByText(/read-only.*archived/i)).toBeInTheDocument()
  })

  it('Back button calls onBack', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }))

    render(<ReviewView sessionId="arc-1" onBack={onBack} />, { wrapper })

    await user.click(screen.getByRole('button', { name: /back to dashboard/i }))

    expect(onBack).toHaveBeenCalledOnce()
  })

  it('calls useSessionTranscript with enabled=true and a bounded initial window', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({
      data: undefined,
      isLoading: true,
    } as ReturnType<typeof hooks.useSessionTranscript>)

    render(<ReviewView sessionId="arc-99" onBack={vi.fn()} />, { wrapper })

    expect(vi.mocked(hooks.useSessionTranscript)).toHaveBeenCalledWith('arc-99', true, 100)
  })

  it('pages in older turns when scrolled to the top', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({
      data: {
        events: [
          { type: 'user', turnId: 'turn-1', text: 'Hello world' },
          { type: 'turnStart', turnId: 'turn-2' },
          { type: 'textDelta', turnId: 'turn-2', text: 'Hi there!' },
          { type: 'turnEnd', turnId: 'turn-2' },
        ],
        earliestTurn: 5,
        totalTurns: 30,
      },
      isLoading: false,
    } as ReturnType<typeof hooks.useSessionTranscript>)
    vi.mocked(fetchOlderTurns).mockResolvedValue({ events: [], earliestTurn: 0, totalTurns: 30 })

    const { container } = render(<ReviewView sessionId="arc-1" onBack={vi.fn()} />, { wrapper })

    // The transcript scroller (mirrors TranscriptView.test.tsx's approach).
    const scroller = container.querySelector('.overflow-y-auto') as HTMLElement
    expect(scroller).toBeTruthy()
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    fireEvent.scroll(scroller)

    expect(fetchOlderTurns).toHaveBeenCalledWith('arc-1', 5)
  })

  it('does not page when already at the session start', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({
      data: {
        events: [
          { type: 'user', turnId: 'turn-1', text: 'Hello world' },
          { type: 'turnStart', turnId: 'turn-2' },
          { type: 'textDelta', turnId: 'turn-2', text: 'Hi there!' },
          { type: 'turnEnd', turnId: 'turn-2' },
        ],
        earliestTurn: 0,
        totalTurns: 1,
      },
      isLoading: false,
    } as ReturnType<typeof hooks.useSessionTranscript>)

    const { container } = render(<ReviewView sessionId="arc-1" onBack={vi.fn()} />, { wrapper })

    const scroller = container.querySelector('.overflow-y-auto') as HTMLElement
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    fireEvent.scroll(scroller)

    expect(fetchOlderTurns).not.toHaveBeenCalled()
  })

  it('renders an Unarchive button', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }))

    render(<ReviewView sessionId="arc-1" onBack={vi.fn()} />, { wrapper })

    expect(screen.getByRole('button', { name: /restore this session from archive/i })).toBeInTheDocument()
  })

  it('calls unarchive.mutate with sessionId when Unarchive is clicked', async () => {
    const user = userEvent.setup()
    const mutate = vi.fn()
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue({ mutate, isPending: false } as ReturnType<typeof hooks.useUnarchiveSession>)
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }))

    render(<ReviewView sessionId="arc-42" onBack={vi.fn()} />, { wrapper })

    await user.click(screen.getByRole('button', { name: /restore this session from archive/i }))

    expect(mutate).toHaveBeenCalledWith('arc-42', expect.objectContaining({ onSuccess: expect.any(Function) }))
  })

  it('shows "Restoring…" and disables button while unarchive is pending', () => {
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue({ mutate: vi.fn(), isPending: true } as ReturnType<typeof hooks.useUnarchiveSession>)
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }))

    render(<ReviewView sessionId="arc-1" onBack={vi.fn()} />, { wrapper })

    const btn = screen.getByRole('button', { name: /restore this session from archive/i })
    expect(btn).toBeDisabled()
    expect(btn).toHaveTextContent('Restoring…')
  })
})
