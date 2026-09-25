import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import type { AssistantGapsReport } from '@/api/assistant'
import { AssistantGapsTable } from './AssistantGapsTable'

const state: { data?: AssistantGapsReport; isLoading: boolean; isError: boolean } = {
  isLoading: false,
  isError: false,
}
const useAssistantGaps = vi.fn<(days?: number) => typeof state>(() => state)

vi.mock('@/api/assistant', () => ({
  useAssistantGaps: (days?: number) => useAssistantGaps(days),
}))

beforeEach(() => {
  state.data = undefined
  state.isLoading = false
  state.isError = false
  useAssistantGaps.mockClear()
})

describe('AssistantGapsTable', () => {
  it('asks for the last 30 days and renders rows in the order the server sorted them', () => {
    const recent = new Date(Date.now() - 2 * 3600_000).toISOString()
    state.data = {
      days: 30,
      total: 7,
      gaps: [
        { capability: 'rotate github token', count: 5, lastSeen: recent },
        { capability: 'restart runner', count: 2, lastSeen: '2026-09-01T00:00:00Z' },
      ],
    }
    render(<AssistantGapsTable />)
    expect(useAssistantGaps).toHaveBeenCalledWith(30)
    expect(screen.getByRole('heading', { name: 'Assistant gaps' })).toBeInTheDocument()
    expect(screen.getByText('Requests the assistant could not act on, last 30 days')).toBeInTheDocument()
    const table = screen.getByRole('table', { name: 'Assistant gaps' })
    const headers = within(table).getAllByRole('columnheader').map((h) => h.textContent)
    expect(headers).toEqual(['Capability', 'Reports', 'Last seen'])
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((r) => within(r).getAllByRole('cell')[0].textContent))
      .toEqual(['rotate github token', 'restart runner'])
    expect(within(rows[0]).getByText('5')).toBeInTheDocument()
    expect(within(rows[0]).getByText('2h ago')).toBeInTheDocument()
  })

  it('says so when nothing was reported', () => {
    state.data = { days: 30, total: 0, gaps: [] }
    render(<AssistantGapsTable />)
    expect(screen.getByText('No gaps reported in the last 30 days.')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('shows an error banner rather than an empty claim when the fetch failed', () => {
    state.isError = true
    render(<AssistantGapsTable />)
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load assistant gaps.')
    expect(screen.queryByText('No gaps reported in the last 30 days.')).not.toBeInTheDocument()
  })

  it('shows a skeleton while loading', () => {
    state.isLoading = true
    render(<AssistantGapsTable />)
    expect(screen.getByRole('status', { name: 'Loading assistant gaps' })).toBeInTheDocument()
    expect(screen.queryByText('No gaps reported in the last 30 days.')).not.toBeInTheDocument()
  })
})
