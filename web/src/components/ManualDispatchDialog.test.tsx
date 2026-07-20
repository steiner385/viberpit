import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ManualDispatchDialog } from './ManualDispatchDialog'

const mutate = vi.fn()

const allCandidates = [
  { ref: 'o/r#7', number: 7, title: 'Fix seven', repo: 'o/r', body: '', url: '' },
  { ref: 'o/r#8', number: 8, title: 'Fix eight', repo: 'o/r', body: '', url: '' },
  { ref: 'a/b#1', number: 1, title: 'Alpha task', repo: 'a/b', body: '', url: '' },
]

let mockCandidates = [...allCandidates]

vi.mock('@/api/hooks', () => ({
  useBacklogCandidates: () => ({ data: { candidates: mockCandidates }, isLoading: false }),
  useDispatchBacklogGroup: () => ({ mutateAsync: mutate, isPending: false }),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

describe('ManualDispatchDialog', () => {
  const onClose = vi.fn()

  beforeEach(() => {
    mockCandidates = [...allCandidates]
    mutate.mockReset()
    onClose.mockReset()
    mutate.mockResolvedValue({ status: 'spawned', session_id: 's-1', session_name: 'backlog o/r#7' })
  })

  it('renders nothing when closed', () => {
    const { container } = render(<ManualDispatchDialog open={false} onClose={onClose} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders candidate groups by repo when open', () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    expect(screen.getByText('o/r')).toBeInTheDocument()
    expect(screen.getByText('a/b')).toBeInTheDocument()
    expect(screen.getByText(/Fix seven/)).toBeInTheDocument()
  })

  it('dispatch button is disabled when nothing selected', () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    expect(screen.getByRole('button', { name: /dispatch 0/i })).toBeDisabled()
  })

  it('selecting issues enables dispatch and calls mutation with correct refs', async () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    fireEvent.click(screen.getByLabelText('select o/r#7'))
    fireEvent.click(screen.getByLabelText('select o/r#8'))
    const btn = screen.getByRole('button', { name: /dispatch 2/i })
    expect(btn).toBeEnabled()
    fireEvent.click(btn)
    await waitFor(() =>
      expect(mutate).toHaveBeenCalledWith(expect.arrayContaining(['o/r#7', 'o/r#8']))
    )
  })

  it('filters candidates by search text', () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    fireEvent.change(screen.getByPlaceholderText(/filter/i), { target: { value: 'Alpha' } })
    expect(screen.getByText(/Alpha task/)).toBeInTheDocument()
    expect(screen.queryByText(/Fix seven/)).toBeNull()
  })

  it('calls onClose after successful dispatch', async () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    fireEvent.click(screen.getByLabelText('select o/r#7'))
    fireEvent.click(screen.getByRole('button', { name: /dispatch 1/i }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('shows empty state when no candidates', () => {
    mockCandidates = []
    render(<ManualDispatchDialog open onClose={onClose} />)
    expect(screen.getByText(/no eligible issues/i)).toBeInTheDocument()
  })
})
