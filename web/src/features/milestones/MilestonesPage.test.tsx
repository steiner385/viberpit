import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { MilestonesPage } from './MilestonesPage'
import { ago, ladderView } from './__fixtures__/ladder'

const q = vi.hoisted(() => ({
  milestones: { data: undefined as unknown, isLoading: false, isError: false, refetch: () => {} },
  project: { data: undefined as unknown },
  refresh: { mutate: vi.fn(), reset: vi.fn(), isPending: false, data: undefined as unknown },
}))
vi.mock('@/api/hooks', () => ({ useProject: () => q.project }))
vi.mock('@/api/milestones', () => ({
  useMilestones: () => q.milestones,
  useRefreshMilestones: () => q.refresh,
}))

const tree = () => (
  <MemoryRouter initialEntries={['/roadmap/kindash/milestones']}>
    <Routes><Route path="/roadmap/:productSlug/milestones" element={<MilestonesPage />} /></Routes>
  </MemoryRouter>
)
const mount = () => render(tree())

beforeEach(() => {
  q.milestones = { data: ladderView(), isLoading: false, isError: false, refetch: () => {} }
  q.project = { data: { name: 'KinDash', repos: [{ id: 'r', repo: 'cairnea/KinDash', isPrimary: true }] } }
  q.refresh = { mutate: vi.fn(), reset: vi.fn(), isPending: false, data: undefined }
})

describe('MilestonesPage', () => {
  it('leads with the header strip numbers', () => {
    mount()
    const strip = screen.getByRole('region', { name: 'Where we are' })
    expect(strip).toHaveTextContent('You are here: Phase 0 · Household gate')
    expect(strip).toHaveTextContent('1 of 4 gate conditions met · 1 unknown')
    expect(strip).toHaveTextContent('1 in flight')
    expect(strip).toHaveTextContent('Updated 4m ago')
  })

  it('renders one card per milestone, the current one marked', () => {
    mount()
    const ladder = screen.getByRole('list', { name: 'Milestones' })
    const cards = within(ladder).getAllByRole('listitem', { name: /Phase \d/ })
    expect(cards).toHaveLength(4)
    expect(cards[0]).toHaveAttribute('aria-current', 'step')
  })

  it('shows the no-ladder EmptyState naming the definition file', () => {
    q.milestones.data = { ladder: null }
    mount()
    expect(screen.getByText(/docs\/product\/milestones\.yaml/)).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Milestones' })).toBeNull()
  })

  it('shows the parse-error banner with a short sha link', () => {
    q.milestones.data = ladderView({ parseError: 'line 4: unknown kind "nope"', parseErrorSha: 'deadbeefcafe1234' })
    mount()
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('line 4: unknown kind "nope"')
    expect(within(alert).getByRole('link', { name: 'deadbee' })).toHaveAttribute(
      'href', 'https://github.com/cairnea/KinDash/blob/deadbeefcafe1234/docs/product/milestones.yaml')
  })

  it('warns when the current milestone was removed from the file', () => {
    q.milestones.data = ladderView({ currentRemoved: true })
    mount()
    expect(screen.getByText('The current milestone was removed from the definition file')).toBeInTheDocument()
  })

  it('with automatic refresh off: a banner linking the automation, Refresh disabled with the reason', () => {
    q.milestones.data = ladderView({ autoRefresh: false })
    mount()
    expect(screen.getByRole('link', { name: /Milestones Refresh/ })).toHaveAttribute('href', '/delivery/automations/milestones-refresh')
    const btn = screen.getByRole('button', { name: 'Refresh' })
    expect(btn).toBeDisabled()
    expect(btn).toHaveAccessibleDescription(/Automatic refresh is off/)
  })

  it('Refresh requests a refresh; a 429 says when to try again', async () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(q.refresh.mutate).toHaveBeenCalled()
    q.refresh.data = { refreshed: false, retryAfter: 42 }
    mount()
    await waitFor(() => expect(screen.getByText('Try again in 42 s')).toBeInTheDocument())
  })

  it('caps the ladder at four columns of at least 15rem; Numbers sits beside it only at 2xl', () => {
    mount()
    const ladder = screen.getByRole('list', { name: 'Milestones' })
    const cols = ladder.style.gridTemplateColumns
    expect(cols).toContain('auto-fit')
    expect(cols).toContain('15rem')
    expect(cols).toContain('/ 4')
    const layout = ladder.parentElement!
    expect(layout.className).toContain('2xl:grid-cols-')
    expect(layout.className).not.toMatch(/(^|\s)xl:grid-cols-/)
  })

  it('a parse error with a last good version says it is showing it', () => {
    q.milestones.data = ladderView({ parseError: 'bad', parseErrorSha: 'deadbeef' })
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Showing the last good version.')
  })

  it('a parse error before any version parsed says so instead', () => {
    q.milestones.data = { ...ladderView({ parseError: 'bad', parseErrorSha: 'deadbeef' }), milestones: [] }
    mount()
    const alert = screen.getByRole('alert')
    expect(alert).not.toHaveTextContent('last good version')
    expect(alert).toHaveTextContent('No version of it has parsed yet.')
  })

  it('clears the refresh outcome when newer numbers arrive', () => {
    q.refresh.data = { refreshed: true }
    const { rerender } = mount()
    expect(screen.getByText('Refresh requested')).toBeInTheDocument()
    rerender(tree())
    expect(q.refresh.reset).not.toHaveBeenCalled()
    q.milestones = { ...q.milestones, data: ladderView({ updatedAt: ago(1) }) }
    rerender(tree())
    expect(q.refresh.reset).toHaveBeenCalledTimes(1)
  })

  it('clears a 429 outcome once retryAfter has passed', () => {
    vi.useFakeTimers()
    try {
      q.refresh.data = { refreshed: false, retryAfter: 42 }
      mount()
      act(() => { vi.advanceTimersByTime(41_000) })
      expect(q.refresh.reset).not.toHaveBeenCalled()
      act(() => { vi.advanceTimersByTime(1_000) })
      expect(q.refresh.reset).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
