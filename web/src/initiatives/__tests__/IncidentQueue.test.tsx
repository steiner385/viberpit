import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/sdlc', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/sdlc')>()
  return { ...actual, fetchIncidents: vi.fn(), actOnIncident: vi.fn() }
})

import { IncidentQueue } from '../IncidentQueue'
import { ApiRequestError } from '@/api/client'
import * as sdlc from '@/api/sdlc'

const mocked = sdlc as unknown as {
  fetchIncidents: ReturnType<typeof vi.fn>
  actOnIncident: ReturnType<typeof vi.fn>
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const INCIDENT: sdlc.Incident = {
  id: 'i1', key: 'pr_cycle_time_hours:we1:high', metric: 'ccswitch-pr-cycle-time',
  source: 'pr_cycle_time_hours', repo: 'steiner385/ccswitch',
  rule: 'we1', sigma: 3, side: 'high', tier: 'propose', response: 'propose',
  value: 51.2345, baselineMean: 10.5, baselineStddev: 0.5, baselineN: 30,
  observedAt: '2026-09-05', summary: 'cycle time above 3σ', evidence: [],
  status: 'open', dismissedReason: null, scheduledFor: null, sessionId: null,
  initiativeSlug: 'incident-cycle-time', occurrences: 1,
  createdAt: null, updatedAt: null,
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('IncidentQueue', () => {
  it('renders nothing at all when there are no incidents', async () => {
    mocked.fetchIncidents.mockResolvedValue([])
    const { container } = render(<IncidentQueue />, { wrapper })
    await waitFor(() => expect(mocked.fetchIncidents).toHaveBeenCalled())
    // A permanent "no incidents" panel would be furniture on a healthy system.
    expect(container.querySelector('.incident-queue')).toBeNull()
  })

  it('shows the breach with its severity, baseline arithmetic and rule in words', async () => {
    mocked.fetchIncidents.mockResolvedValue([INCIDENT])
    render(<IncidentQueue />, { wrapper })

    await waitFor(() =>
      expect(screen.getByText('ccswitch-pr-cycle-time')).toBeInTheDocument())
    expect(screen.getByText('3σ')).toBeInTheDocument()
    expect(screen.getByText('1 point beyond 3σ')).toBeInTheDocument()
    // Value rounded to 3 significant figures, baseline shown alongside it.
    expect(screen.getByText('51.2')).toBeInTheDocument()
    expect(screen.getByText(/vs baseline\s*10.5/)).toBeInTheDocument()
    expect(screen.getByText(/n=30/)).toBeInTheDocument()
  })

  it('queues an incident for intake via the triage-now tap', async () => {
    mocked.fetchIncidents.mockResolvedValue([INCIDENT])
    mocked.actOnIncident.mockResolvedValue({ ...INCIDENT, status: 'queued' })
    render(<IncidentQueue />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: 'Triage now' }))
    await waitFor(() => expect(mocked.actOnIncident).toHaveBeenCalledWith({
      id: 'i1', action: 'triage_now',
    }))
  })

  it('snoozes with a future timestamp', async () => {
    mocked.fetchIncidents.mockResolvedValue([INCIDENT])
    mocked.actOnIncident.mockResolvedValue({ ...INCIDENT, status: 'scheduled' })
    render(<IncidentQueue />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: /Snooze tomorrow/ }))
    await waitFor(() => expect(mocked.actOnIncident).toHaveBeenCalled())
    const call = mocked.actOnIncident.mock.calls[0]![0] as sdlc.IncidentActionInput
    expect(call.action).toBe('schedule')
    // The API refuses a past time, so the client must never send one.
    expect(new Date(call.scheduledFor!).getTime()).toBeGreaterThan(Date.now())
  })

  it('requires a reason before it will dismiss', async () => {
    mocked.fetchIncidents.mockResolvedValue([INCIDENT])
    mocked.actOnIncident.mockResolvedValue({ ...INCIDENT, status: 'dismissed' })
    render(<IncidentQueue />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss…' }))
    const submit = screen.getByRole('button', { name: 'Dismiss' })
    expect(submit).toBeDisabled()
    expect(mocked.actOnIncident).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('dismissal reason'),
                     { target: { value: 'seasonal' } })
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(mocked.actOnIncident).toHaveBeenCalledWith({
      id: 'i1', action: 'dismiss', reason: 'seasonal',
    }))
  })

  it('renders a refusal inline rather than swallowing it', async () => {
    mocked.fetchIncidents.mockResolvedValue([INCIDENT])
    mocked.actOnIncident.mockRejectedValue(
      new ApiRequestError(409, { detail: { message: 'already adopted' } }))
    render(<IncidentQueue />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: 'Triage now' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('already adopted')
  })

  it('hides settled incidents behind a toggle and offers no actions on them', async () => {
    mocked.fetchIncidents.mockResolvedValue([
      { ...INCIDENT, id: 'i2', status: 'dismissed', dismissedReason: 'seasonal' },
    ])
    render(<IncidentQueue />, { wrapper })

    const toggle = await screen.findByRole('button', { name: /Show 1 settled/ })
    expect(screen.queryByRole('button', { name: 'Triage now' })).toBeNull()
    expect(screen.getByText(/every watched band is in control/i)).toBeInTheDocument()

    fireEvent.click(toggle)
    expect(screen.getByText('Dismissed: seasonal')).toBeInTheDocument()
    // A settled incident is not re-dispositionable — the API refuses it too.
    expect(screen.queryByRole('button', { name: 'Triage now' })).toBeNull()
  })

  it('counts repeat observations of an ongoing breach', async () => {
    mocked.fetchIncidents.mockResolvedValue([{ ...INCIDENT, occurrences: 4 }])
    render(<IncidentQueue />, { wrapper })
    expect(await screen.findByText('×4')).toBeInTheDocument()
  })

  it('names the initiative once intake has adopted the incident', async () => {
    mocked.fetchIncidents.mockResolvedValue([{ ...INCIDENT, status: 'intake' }])
    render(<IncidentQueue />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: /Show 1 settled/ }))
    expect(screen.getByText('incident-cycle-time')).toBeInTheDocument()
    expect(screen.getByText('Adopted by intake')).toBeInTheDocument()
  })
})
