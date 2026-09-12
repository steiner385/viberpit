import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { BacklogGroupsPanel } from './BacklogGroupsPanel'
import type { BacklogGroup } from '@/api/automations'

const mutate = vi.fn()
const baseGroup = (): BacklogGroup => ({
  id: 1,
  repo: 'o/r',
  refs: ['o/r#7', 'o/r#8'],
  titles: { 'o/r#7': 'Fix seven', 'o/r#8': 'Fix eight' },
  rationale: 'same subsystem',
  rank: 0,
  generation: 'g1',
  createdAt: '2026-07-20T10:00:00Z',
  consumedAt: null,
})
let currentGroups = [baseGroup()]

vi.mock('@/api/hooks', () => ({
  useBacklogGroups: () => ({
    data: { generation: 'g1', createdAt: '2026-07-20T10:00:00Z', groups: currentGroups },
    isLoading: false,
  }),
  useDispatchBacklogGroup: () => ({ mutateAsync: mutate, isPending: false }),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

describe('BacklogGroupsPanel', () => {
  beforeEach(() => {
    currentGroups = [baseGroup()]
    mutate.mockReset()
    mutate.mockResolvedValue({
      status: 'spawned',
      session_id: 's-1',
      session_name: 'backlog o/r#7',
    })
  })

  it('renders group rows with issue links', () => {
    render(<BacklogGroupsPanel enabled />)
    expect(screen.getByText(/Fix seven/)).toBeInTheDocument()
    expect(screen.getByText(/Fix eight/)).toBeInTheDocument()
  })

  it('shows dispatch button for unconsumed groups', () => {
    render(<BacklogGroupsPanel enabled />)
    expect(screen.getByRole('button', { name: /dispatch/i })).toBeInTheDocument()
  })

  it('hides dispatch button for consumed groups', () => {
    currentGroups = [{ ...baseGroup(), consumedAt: '2026-07-20T11:00:00Z' }]
    render(<BacklogGroupsPanel enabled />)
    expect(screen.queryByRole('button', { name: /dispatch/i })).toBeNull()
    expect(screen.getByText(/picked up/i)).toBeInTheDocument()
  })

  it('clicking dispatch calls mutateAsync with the group refs', async () => {
    render(<BacklogGroupsPanel enabled />)
    fireEvent.click(screen.getByRole('button', { name: /dispatch/i }))
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(['o/r#7', 'o/r#8']))
  })

  it('calls onOpenSession with session_id on successful spawn', async () => {
    const onOpenSession = vi.fn()
    render(<BacklogGroupsPanel enabled onOpenSession={onOpenSession} />)
    fireEvent.click(screen.getByRole('button', { name: /dispatch/i }))
    await waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('s-1'))
  })

  it('shows the failure inline instead of a toast', async () => {
    const { toast } = await import('sonner')
    mutate.mockRejectedValue(new Error('network error'))
    render(<BacklogGroupsPanel enabled />)
    fireEvent.click(screen.getByRole('button', { name: /dispatch/i }))
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('Failed to dispatch group'),
    )
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('shows a non-spawned status inline instead of a toast', async () => {
    const { toast } = await import('sonner')
    mutate.mockResolvedValue({ status: 'no_capacity' })
    render(<BacklogGroupsPanel enabled />)
    fireEvent.click(screen.getByRole('button', { name: /dispatch/i }))
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('Dispatch failed: no_capacity'),
    )
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('clears the inline error on the next dispatch attempt', async () => {
    mutate.mockRejectedValueOnce(new Error('network error'))
    render(<BacklogGroupsPanel enabled />)
    fireEvent.click(screen.getByRole('button', { name: /dispatch/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /dispatch/i }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })
})
