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

  it('renders in normal flow, never as a fixed overlay', () => {
    // Regression (#620): as a `fixed top-0 z-50` overlay this banner sat on the
    // session top bar and captured the click meant for "Back to session list"
    // (dashboard e2e T044 — "…subtree intercepts pointer events"). It also shared
    // that band with the toaster. In flow (inside BannerStack) it pushes content
    // down instead of covering it, so there is nothing beneath it to intercept.
    mockDaemon(false, false)
    render(<DaemonRestartBanner />)
    const el = screen.getByRole('status')
    expect(el.className).not.toMatch(/\bfixed\b/)
    expect(el.className).not.toMatch(/\bz-\S+/)
  })
})
