import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { DaemonRestartBanner } from './DaemonRestartBanner'
import * as health from '@/hooks/useDaemonReachable'
import * as epochRestart from '@/hooks/useEpochRestart'

afterEach(() => vi.restoreAllMocks())

function mockDaemon(ok: boolean, recovering = false) {
  vi.spyOn(health, 'useDaemonReachable').mockReturnValue(ok)
  vi.spyOn(epochRestart, 'useEpochRestart').mockReturnValue(recovering)
}

describe('DaemonRestartBanner', () => {
  it('renders nothing while the daemon is healthy and not recovering', () => {
    mockDaemon(true, false)
    const { container } = render(<DaemonRestartBanner />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the restart notice when the daemon is unreachable (health polling)', () => {
    mockDaemon(false, false)
    render(<DaemonRestartBanner />)
    expect(screen.getByRole('status')).toHaveTextContent(/back in a moment/i)
  })

  it('shows the restart notice immediately on epoch-change (close-1012) even before poll fires', () => {
    mockDaemon(true, true)
    render(<DaemonRestartBanner />)
    expect(screen.getByRole('status')).toHaveTextContent(/back in a moment/i)
  })

  it('does not intercept pointer events on the UI beneath it', () => {
    // Regression (#620): this fixed top-0 z-50 banner overlaid the session top bar
    // and captured the click meant for "Back to session list" (dashboard e2e T044 —
    // "…subtree intercepts pointer events"). A role="status" notice has no interactive
    // children, so it must let clicks pass through to the controls underneath.
    mockDaemon(false, false)
    render(<DaemonRestartBanner />)
    expect(screen.getByRole('status')).toHaveClass('pointer-events-none')
  })
})
