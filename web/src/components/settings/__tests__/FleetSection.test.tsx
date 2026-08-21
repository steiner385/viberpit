import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import { FleetSection } from '../FleetSection'

const hosts = [
  { name: 'dobby', endpoint: 'unix:///run/ccswitch/sessiond.sock', enabled: true,
    weight: 100, maxSessions: null, healthy: true, activeSessions: 1,
    loadAvg: 0.4, memAvailableMb: 8192, sha: 'abc1234', protocol: 1,
    lastProbeAt: '2026-08-21T12:00:00', lastError: null },
  { name: 'rocky', endpoint: 'https://rocky.tail0dd570.ts.net:9443', enabled: true,
    weight: 100, maxSessions: 4, healthy: false, activeSessions: 0,
    loadAvg: null, memAvailableMb: null, sha: null, protocol: null,
    lastProbeAt: '2026-08-21T12:00:00', lastError: 'ConnectionError: refused' },
  { name: 'fresh', endpoint: 'https://fresh:9443', enabled: true, weight: 100,
    maxSessions: null, healthy: null, activeSessions: null, loadAvg: null,
    memAvailableMb: null, sha: null, protocol: null, lastProbeAt: null,
    lastError: null },
]

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(async () => ({ hosts })),
    put: vi.fn(async () => hosts[0]),
  },
}))

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

beforeEach(() => vi.clearAllMocks())

describe('FleetSection', () => {
  it('lists every host', async () => {
    wrap(<FleetSection />)
    await waitFor(() => expect(screen.getByText('dobby')).toBeInTheDocument())
    expect(screen.getByText('rocky')).toBeInTheDocument()
  })

  it('surfaces the probe error for an unhealthy host', async () => {
    // Without this an operator sees "unhealthy" with no way to learn why.
    wrap(<FleetSection />)
    await waitFor(() =>
      expect(screen.getByText(/ConnectionError: refused/)).toBeInTheDocument())
  })

  it('shows capacity as used-of-limit when a limit is set', async () => {
    wrap(<FleetSection />)
    await waitFor(() => expect(screen.getByText('0 / 4')).toBeInTheDocument())
  })

  it('distinguishes never-probed from unhealthy', async () => {
    // healthy=null must not read as "down" — it means we have not asked yet.
    wrap(<FleetSection />)
    await waitFor(() => expect(screen.getByText('fresh')).toBeInTheDocument())
    expect(screen.getByText(/not yet probed/i)).toBeInTheDocument()
  })

  it('shows each host running sha so version skew is visible', async () => {
    // Version skew between edge and daemon broke repo listing on 2026-08-21 and
    // was invisible until a dropdown failed. Surfacing sha makes it observable.
    wrap(<FleetSection />)
    await waitFor(() => expect(screen.getByText('abc1234')).toBeInTheDocument())
  })
})
