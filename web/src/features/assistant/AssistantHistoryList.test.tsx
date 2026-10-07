import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AssistantHistoryList } from './AssistantHistoryList'

const mutate = vi.fn()
const state = {
  history: { data: [] as Array<{ id: string; snippet: string | null; archivedAt: string | null }>, isLoading: false },
  resume: { isPending: false, isError: false },
}

vi.mock('@/api/assistant', () => ({
  useAssistantHistory: (enabled: boolean) => ({ ...state.history, enabled }),
  useResumeAssistantSession: () => ({ mutate, ...state.resume }),
}))

beforeEach(() => {
  mutate.mockReset()
  mutate.mockImplementation((_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.())
  
  state.history = { data: [], isLoading: false }
  state.resume = { isPending: false, isError: false }
})

describe('AssistantHistoryList', () => {
  it('renders nothing when closed', () => {
    render(<AssistantHistoryList open={false} onClose={vi.fn()} />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows a loading state while the history query is in flight', () => {
    state.history = { data: [], isLoading: true }
    render(<AssistantHistoryList open onClose={vi.fn()} />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows an empty state when there is no history', () => {
    render(<AssistantHistoryList open onClose={vi.fn()} />)
    expect(screen.getByText('No previous conversations yet.')).toBeInTheDocument()
  })

  it('renders each archived thread with its snippet and relative time', () => {
    state.history = {
      isLoading: false,
      data: [
        { id: 'h1', snippet: 'fix the flaky test', archivedAt: new Date(Date.now() - 60_000).toISOString() },
        { id: 'h2', snippet: null, archivedAt: null },
      ],
    }
    render(<AssistantHistoryList open onClose={vi.fn()} />)
    expect(screen.getByText('fix the flaky test')).toBeInTheDocument()
    expect(screen.getByText('Untitled conversation')).toBeInTheDocument()
    expect(screen.getByText(/ago/)).toBeInTheDocument()
  })

  it('resumes the clicked thread and closes the popover', async () => {
    const onClose = vi.fn()
    state.history = {
      isLoading: false,
      data: [{ id: 'h1', snippet: 'fix the flaky test', archivedAt: null }],
    }
    render(<AssistantHistoryList open onClose={onClose} />)
    fireEvent.click(screen.getByText('fix the flaky test'))
    expect(mutate).toHaveBeenCalledWith('h1', expect.objectContaining({ onSuccess: expect.any(Function) }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('shows an error message when resume fails', () => {
    state.resume = { isPending: false, isError: true }
    state.history = {
      isLoading: false,
      data: [{ id: 'h1', snippet: 'fix the flaky test', archivedAt: null }],
    }
    render(<AssistantHistoryList open onClose={vi.fn()} />)
    expect(screen.getByRole('alert')).toHaveTextContent(/couldn/i)
  })
})
