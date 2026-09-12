import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('./NotificationsToggle', () => ({ NotificationsToggle: () => null }))

vi.mock('@/api/client', () => ({
  ApiRequestError: class ApiRequestError extends Error {
    status: number
    body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`)
      this.status = status
      this.body = body
    }
  },
  api: {
    get: vi.fn(),
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    patch: vi.fn(),
    del: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

import { NotificationsSection } from './NotificationsSection'
import { api } from '@/api/client'

const channels = [
  { id: 'ch1', name: 'Slack #alerts', kind: 'slack', url: 'https://hooks.slack.com/x', headers: null, enabled: true, createdAt: '2026-01-01' },
]
const rules = [
  {
    id: 'r1', enabled: true, eventClass: 'pr_broke', scopeRepo: 'me/app', scopeAccount: null,
    scopeAutomation: null, action: 'channel', channelId: 'ch1', critical: true, priority: 10, createdAt: '2026-01-01',
  },
]
const quiet = { start: '22:00', end: '07:00' }

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <NotificationsSection />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === '/notifications/channels') return Promise.resolve(channels)
    if (path === '/notifications/rules') return Promise.resolve(rules)
    if (path === '/notifications/quiet-hours') return Promise.resolve(quiet)
    return Promise.resolve({})
  })
  vi.mocked(api.post).mockResolvedValue({})
  vi.mocked(api.put).mockResolvedValue({})
  vi.mocked(api.del).mockResolvedValue({ ok: true })
})

describe('NotificationsSection', () => {
  it('renders quiet-hours, channels, and rules from the mocked GETs', async () => {
    renderSection()
    // quiet hours prefilled
    await waitFor(() => {
      expect((screen.getByLabelText('Start') as HTMLInputElement).value).toBe('22:00')
    })
    expect((screen.getByLabelText('End') as HTMLInputElement).value).toBe('07:00')
    // channel row
    expect(await screen.findByText('Slack #alerts')).toBeTruthy()
    // rule row (event label appears in the row span and the Event select option)
    expect((await screen.findAllByText('PR broke')).length).toBeGreaterThan(0)
    expect(screen.getByText('critical')).toBeTruthy()
  })

  it('adding a channel calls api.post("/notifications/channels", ...)', async () => {
    renderSection()
    await screen.findByText('Slack #alerts')

    await userEvent.type(screen.getByLabelText('Name'), 'Webhook')
    await userEvent.type(screen.getByLabelText('URL'), 'https://example.com/hook')
    await userEvent.click(screen.getByRole('button', { name: /add channel/i }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/notifications/channels',
        expect.objectContaining({ name: 'Webhook', url: 'https://example.com/hook' }),
      )
    })
  })

  it('shows an inline error and does not submit on bad headers JSON', async () => {
    renderSection()
    await screen.findByText('Slack #alerts')

    await userEvent.type(screen.getByLabelText('Name'), 'Webhook')
    await userEvent.type(screen.getByLabelText('URL'), 'https://example.com/hook')
    await userEvent.type(screen.getByLabelText(/Headers/i), '{{not json')
    await userEvent.click(screen.getByRole('button', { name: /add channel/i }))

    expect(await screen.findByText(/not valid JSON/i)).toBeTruthy()
    expect(api.post).not.toHaveBeenCalledWith('/notifications/channels', expect.anything())
  })

  it('adding a rule with action "channel" includes channelId', async () => {
    renderSection()
    await screen.findAllByText('PR broke')

    await userEvent.selectOptions(screen.getByLabelText('Action'), 'channel')
    // channel select now visible
    await userEvent.selectOptions(await screen.findByLabelText('Channel'), 'ch1')
    await userEvent.click(screen.getByRole('button', { name: /add rule/i }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/notifications/rules',
        expect.objectContaining({ action: 'channel', channelId: 'ch1' }),
      )
    })
  })

  it('"Test routing" calls api.post("/notifications/test", ...) and renders the decision', async () => {
    vi.mocked(api.post).mockResolvedValue({
      action: 'channel', channelId: 'ch1', deliver: true, suppressedByQuiet: false, matchedRuleId: 'r1',
    })
    renderSection()
    await screen.findAllByText('PR broke')

    await userEvent.selectOptions(screen.getByLabelText('Event'), 'pr_broke')
    await userEvent.click(screen.getByRole('button', { name: /test routing/i }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/notifications/test',
        expect.objectContaining({ eventClass: 'pr_broke' }),
      )
    })
    const decision = await screen.findByTestId('test-decision')
    expect(decision.textContent).toContain('channel')
    expect(decision.textContent).toContain('r1')
  })

  it('saving quiet hours calls api.put("/notifications/quiet-hours", {start,end})', async () => {
    renderSection()
    await waitFor(() => {
      expect((screen.getByLabelText('Start') as HTMLInputElement).value).toBe('22:00')
    })

    // Get all Save buttons; the quiet-hours Save is the first one
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith(
        '/notifications/quiet-hours',
        { start: '22:00', end: '07:00' },
      )
    })
  })

  it('clearing quiet hours PUTs nulls', async () => {
    renderSection()
    await waitFor(() => {
      expect((screen.getByLabelText('Start') as HTMLInputElement).value).toBe('22:00')
    })

    await userEvent.click(screen.getByRole('button', { name: /^clear$/i }))

    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith(
        '/notifications/quiet-hours',
        { start: null, end: null },
      )
    })
  })
})
