import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import { FleetSection } from '../FleetSection'

const idle = { state: 'idle', step: null, error: null, startedAt: null, target: null }

const hosts = [
  { name: 'dobby', endpoint: 'unix:///run/ccswitch/sessiond.sock', enabled: true,
    weight: 100, maxSessions: null, healthy: true, activeSessions: 1,
    loadAvg: 0.4, memAvailableMb: 8192, sha: 'def5678', protocol: 1,
    lastProbeAt: '2026-08-21T12:00:00', lastError: null,
    sshHost: null, repoPath: null, upgrade: idle },
  // Stale sha + ssh config -> in-app update is offered.
  { name: 'rocky', endpoint: 'https://rocky.tail0dd570.ts.net:9443', enabled: true,
    weight: 100, maxSessions: 4, healthy: false, activeSessions: 0,
    loadAvg: null, memAvailableMb: null, sha: 'abc1234', protocol: null,
    lastProbeAt: '2026-08-21T12:00:00', lastError: 'ConnectionError: refused',
    sshHost: 'deploy@rocky', repoPath: '/srv/ccswitch', upgrade: idle },
  // Stale sha but NO ssh config -> nothing the app can do; no button.
  { name: 'fresh', endpoint: 'https://fresh:9443', enabled: true, weight: 100,
    maxSessions: null, healthy: null, activeSessions: null, loadAvg: null,
    memAvailableMb: null, sha: null, protocol: null, lastProbeAt: null,
    lastError: null, sshHost: null, repoPath: null, upgrade: idle },
]

// The newest known sha comes from the deploy status query (targetSha).
const deployStatus = {
  pending: { behind: false, liveSha: 'def5678', targetSha: 'def5678', commits: [], ci: 'green' },
  deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: '' },
}

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(async (path: string) =>
      path.startsWith('/deploy') ? deployStatus : { hosts }),
    put: vi.fn(async () => hosts[0]),
    post: vi.fn(async () => ({ started: true })),
  },
}))

import { api } from '@/api/client'

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

  it('offers Update only for a stale host WITH ssh config', async () => {
    wrap(<FleetSection />)
    // rocky trails targetSha and has sshHost/repoPath -> button.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'update rocky' })).toBeInTheDocument())
    // dobby is on the newest sha; fresh has no ssh config -> no buttons.
    expect(screen.queryByRole('button', { name: 'update dobby' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'update fresh' })).toBeNull()
  })

  it('confirms, then POSTs the upgrade', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    wrap(<FleetSection />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'update rocky' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'update rocky' }))
    expect(confirm).toHaveBeenCalled()
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/fleet/hosts/rocky/upgrade', {}))
    confirm.mockRestore()
  })

  it('does not POST when the confirm is dismissed', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    wrap(<FleetSection />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'update rocky' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'update rocky' }))
    expect(api.post).not.toHaveBeenCalled()
    confirm.mockRestore()
  })

  it('shows a running upgrade state instead of the button', async () => {
    hosts[1] = { ...hosts[1], upgrade: {
      state: 'running', step: 'pip', error: null,
      startedAt: '2026-09-01T12:00:00Z', target: 'def5678' } }
    wrap(<FleetSection />)
    await waitFor(() => expect(screen.getByText(/updating…/)).toBeInTheDocument())
    expect(screen.getByText(/pip/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'update rocky' })).toBeNull()
    hosts[1] = { ...hosts[1], upgrade: idle }
  })

  it('surfaces a failed upgrade with its error', async () => {
    hosts[1] = { ...hosts[1], upgrade: {
      state: 'failed', step: 'merge', error: 'merge failed (rc=1): diverged',
      startedAt: '2026-09-01T12:00:00Z', target: 'def5678' } }
    wrap(<FleetSection />)
    await waitFor(() =>
      expect(screen.getByText(/update failed: merge failed/)).toBeInTheDocument())
    hosts[1] = { ...hosts[1], upgrade: idle }
  })

  it('shows the checkout sha when it differs from the process sha', async () => {
    const original = hosts[1]
    hosts[1] = { ...hosts[1], sha: 'proc111', checkoutSha: 'chk2222' }
    wrap(<FleetSection />)
    expect(await screen.findByText(/proc111/)).toBeInTheDocument()
    expect(screen.getByText(/checkout chk2222/)).toBeInTheDocument()
    hosts[1] = original
  })
})
