import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { DaemonHealth } from '@/api/types'
import { DaemonHealthIndicator } from './DaemonHealthIndicator'
import { formatUptime, daemonHealthTooltip } from '@/lib/daemonHealth'

const toastWarning = vi.fn()
vi.mock('sonner', () => ({ toast: { warning: (...a: unknown[]) => toastWarning(...a) } }))

const mockHealth = vi.fn<() => { data: DaemonHealth | undefined }>()
vi.mock('@/api/hooks', () => ({
  useDaemonHealth: () => mockHealth(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

function renderIndicator() {
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}><DaemonHealthIndicator /></QueryClientProvider>,
  )
}

beforeEach(() => {
  toastWarning.mockClear()
  mockHealth.mockReset()
})

describe('formatUptime', () => {
  it('formats across units', () => {
    expect(formatUptime(45)).toBe('45s')
    expect(formatUptime(600)).toBe('10m')
    expect(formatUptime(7200)).toBe('2h')
    expect(formatUptime(2 * 86400)).toBe('2d')
    expect(formatUptime(null)).toBe('—')
  })
})

describe('daemonHealthTooltip', () => {
  it('includes restart count and epoch', () => {
    const tip = daemonHealthTooltip({
      available: true, epoch: 'abc-1-x', startedAt: null, uptimeSec: 3600,
      restartCount24h: 3, lastRestartAt: null, lastSeenAgoSec: 5,
    })
    expect(tip).toContain('Restarts (24h): 3')
    expect(tip).toContain('Epoch: abc-1-x')
  })
})

describe('DaemonHealthIndicator', () => {
  it('renders nothing when there is no daemon to watch', () => {
    mockHealth.mockReturnValue({ data: { available: false } as DaemonHealth })
    const { container } = renderIndicator()
    expect(container.querySelector('[data-testid="daemon-health"]')).toBeNull()
  })

  it('shows uptime when healthy (no restarts)', () => {
    mockHealth.mockReturnValue({ data: {
      available: true, epoch: 'e', startedAt: null, uptimeSec: 7200,
      restartCount24h: 0, lastRestartAt: null, lastSeenAgoSec: 2,
    } })
    renderIndicator()
    expect(screen.getByTestId('daemon-health').textContent).toContain('up 2h')
  })

  it('shows a restart count when the daemon has flapped', () => {
    mockHealth.mockReturnValue({ data: {
      available: true, epoch: 'e', startedAt: null, uptimeSec: 60,
      restartCount24h: 9, lastRestartAt: '2026-07-02T12:00:00Z', lastSeenAgoSec: 8,
    } })
    renderIndicator()
    expect(screen.getByTestId('daemon-health').textContent).toContain('9× restarts (24h)')
  })
})
