import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import type { InboxClusters } from '@/api/automations'
import { FeedbackInbox } from './FeedbackInbox'

const fileCluster = vi.fn()
const dismissCluster = vi.fn()
const attachCluster = vi.fn()
const ok = { status: 'ok', level: 'ok', message: 'Done' }

const DATA: InboxClusters = {
  llm: 'applied',
  total: 3,
  clusters: [
    {
      id: 'c1', title: 'Daemon restarts kill verification runs', count: 2, category: 'orchestration',
      impact: 'blocked', firstReported: '2026-10-01T02:00:00Z', lastReported: '2026-10-02T02:00:00Z',
      themed: true, fingerprints: ['fpA', 'fpB'],
      members: [
        { id: 'r2', title: 'Restart killed the run again', category: 'orchestration', impact: 'blocked',
          fingerprint: 'fpB', reportedAt: '2026-10-02T02:00:00Z', status: 'pending', session: 'beta' },
        { id: 'r1', title: 'Daemon restart killed verification', category: 'orchestration', impact: 'degraded',
          fingerprint: 'fpA', reportedAt: '2026-10-01T02:00:00Z', status: 'pending', session: 'alpha' },
      ],
    },
    {
      id: 'c2', title: 'Screenshots do not render', count: 1, category: 'tooling', impact: 'degraded',
      firstReported: '2026-10-01T05:00:00Z', lastReported: '2026-10-01T05:00:00Z', themed: false,
      fingerprints: ['fpC'],
      members: [{ id: 'r3', title: 'Screenshots do not render', category: 'tooling', impact: 'degraded',
        fingerprint: 'fpC', reportedAt: '2026-10-01T05:00:00Z', status: 'pending', session: 'gamma' }],
    },
  ],
}

let clusters: { data?: InboxClusters; isLoading?: boolean; isError?: boolean } = { data: DATA }

vi.mock('@/api/hooks', () => ({
  useFeedbackClusters: () => clusters,
  useFeedbackClusterDraft: (_id: string, ids: string[], enabled: boolean) => ({
    data: enabled ? { ...ok, title: '[session-feedback] Daemon restarts kill verification runs', body: `BODY ${ids.join(',')}` } : undefined,
    isLoading: false, isError: false,
  }),
  useFileFeedbackCluster: () => ({ mutateAsync: fileCluster, isPending: false }),
  useDismissFeedbackCluster: () => ({ mutateAsync: dismissCluster, isPending: false }),
  useAttachFeedbackCluster: () => ({ mutateAsync: attachCluster, isPending: false }),
  useSessions: () => ({ data: [
    { id: 'live1', name: 'fix restarts', status: 'in_progress' },
    { id: 'old', name: 'done long ago', status: 'ended' },
  ] }),
}))

const mount = () => render(<FeedbackInbox automationId="session-feedback" />)

describe('FeedbackInbox', () => {
  beforeEach(() => {
    clusters = { data: DATA }
    fileCluster.mockReset().mockResolvedValue({ ...ok, message: 'Issue filed: #100' })
    dismissCluster.mockReset().mockResolvedValue(ok)
    attachCluster.mockReset().mockResolvedValue(ok)
  })

  it('one row per cluster with its count; members expand', () => {
    mount()
    expect(screen.getByText('3 reports in 2 clusters')).toBeInTheDocument()
    const rows = screen.getAllByRole('listitem').filter((li) => li.dataset.cluster)
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText('Daemon restarts kill verification runs')).toBeInTheDocument()
    expect(within(rows[0]).getByText('2')).toBeInTheDocument()
    expect(screen.queryByText('Restart killed the run again')).toBeNull()
    fireEvent.click(within(rows[0]).getByRole('button', { name: /Daemon restarts kill verification runs/ }))
    expect(screen.getByText('Restart killed the run again')).toBeInTheDocument()
    expect(screen.getByText('Daemon restart killed verification')).toBeInTheDocument()
  })

  it('says when the model pass is still catching up or failed', () => {
    clusters = { data: { ...DATA, llm: 'pending' } }
    const r = mount()
    expect(screen.getByText(/grouping with a model/i)).toBeInTheDocument()
    r.unmount()
    clusters = { data: { ...DATA, llm: 'failed' } }
    mount()
    expect(screen.getByText(/similarity only/i)).toBeInTheDocument()
  })

  it('paused: the clusters stay actionable under the parked notice', () => {
    render(<FeedbackInbox automationId="session-feedback" paused />)
    expect(screen.getByRole('heading', { name: 'Paused — 3 parked' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'File as one issue' })).toHaveLength(2)
  })

  it('empty inbox reads as nothing to do', () => {
    clusters = { data: { clusters: [], llm: 'applied', total: 0 } }
    mount()
    expect(screen.getByText('Nothing needs you.')).toBeInTheDocument()
  })

  it('File as one issue: opens the editor prefilled from the draft and files the edit', async () => {
    mount()
    const row = screen.getAllByRole('listitem').filter((li) => li.dataset.cluster)[0]
    fireEvent.click(within(row).getByRole('button', { name: 'File as one issue' }))
    const dialog = await screen.findByRole('dialog', { name: 'File as one issue' })
    const title = within(dialog).getByLabelText('Title') as HTMLInputElement
    expect(title.value).toBe('[session-feedback] Daemon restarts kill verification runs')
    expect((within(dialog).getByLabelText('Body') as HTMLTextAreaElement).value).toBe('BODY r2,r1')
    fireEvent.change(title, { target: { value: 'Restarts kill runs' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'File issue' }))
    await waitFor(() => expect(fileCluster).toHaveBeenCalledWith(
      { ids: ['r2', 'r1'], title: 'Restarts kill runs', body: 'BODY r2,r1' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('Dismiss: one confirmation dismisses the whole cluster in one request', async () => {
    mount()
    const row = screen.getAllByRole('listitem').filter((li) => li.dataset.cluster)[0]
    fireEvent.click(within(row).getByRole('button', { name: 'Dismiss' }))
    const dialog = await screen.findByRole('dialog', { name: 'Dismiss 2 reports?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(dismissCluster).toHaveBeenCalledTimes(1))
    expect(dismissCluster).toHaveBeenCalledWith({ ids: ['r2', 'r1'] })
  })

  it('Attach: to an existing issue by number', async () => {
    mount()
    const row = screen.getAllByRole('listitem').filter((li) => li.dataset.cluster)[1]
    fireEvent.click(within(row).getByRole('button', { name: 'Attach…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Attach 1 report' })
    fireEvent.change(within(dialog).getByLabelText('Issue number'), { target: { value: '2909' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Attach' }))
    await waitFor(() => expect(attachCluster).toHaveBeenCalledWith({ ids: ['r3'], issue: 2909 }))
  })

  it('Attach: to a live session — ended sessions are not offered', async () => {
    mount()
    const row = screen.getAllByRole('listitem').filter((li) => li.dataset.cluster)[1]
    fireEvent.click(within(row).getByRole('button', { name: 'Attach…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Attach 1 report' })
    fireEvent.click(within(dialog).getByLabelText('A live session'))
    const select = within(dialog).getByLabelText('Session') as HTMLSelectElement
    expect(within(select).queryByText('done long ago')).toBeNull()
    fireEvent.change(select, { target: { value: 'live1' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Attach' }))
    await waitFor(() => expect(attachCluster).toHaveBeenCalledWith({ ids: ['r3'], sessionId: 'live1' }))
  })
})
