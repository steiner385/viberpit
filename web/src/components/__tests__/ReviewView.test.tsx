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
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ReviewView } from '../ReviewView'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks', () => ({
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSessionTranscript: vi.fn(),
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
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({
      data: { events: [] },
      isLoading: false,
    } as ReturnType<typeof hooks.useSessionTranscript>)

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
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({
      data: { events: [] },
      isLoading: false,
    } as ReturnType<typeof hooks.useSessionTranscript>)

    render(<ReviewView sessionId="arc-1" onBack={vi.fn()} />, { wrapper })

    expect(screen.getByText(/read-only.*archived/i)).toBeInTheDocument()
  })

  it('Back button calls onBack', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({
      data: { events: [] },
      isLoading: false,
    } as ReturnType<typeof hooks.useSessionTranscript>)

    render(<ReviewView sessionId="arc-1" onBack={onBack} />, { wrapper })

    await user.click(screen.getByRole('button', { name: /back to dashboard/i }))

    expect(onBack).toHaveBeenCalledOnce()
  })

  it('calls useSessionTranscript with enabled=true', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({
      data: undefined,
      isLoading: true,
    } as ReturnType<typeof hooks.useSessionTranscript>)

    render(<ReviewView sessionId="arc-99" onBack={vi.fn()} />, { wrapper })

    expect(vi.mocked(hooks.useSessionTranscript)).toHaveBeenCalledWith('arc-99', true)
  })
})
