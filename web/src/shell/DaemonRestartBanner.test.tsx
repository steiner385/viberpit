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

  // The copy names the cause (2026-09-29 re-critique): a planned restart is
  // coming back and says so; an unreachable daemon is a failed check the app
  // cannot explain, so it says only what it knows — that it keeps retrying.
  it('names an unreachable daemon as unreachable, and that it retries (health polling)', () => {
    mockDaemon(false, false)
    render(<DaemonRestartBanner />)
    expect(screen.getByRole('status')).toHaveTextContent(/can.t reach the session daemon/i)
    expect(screen.getByRole('status')).toHaveTextContent(/retrying/i)
    expect(screen.getByRole('status')).not.toHaveTextContent(/back in a moment/i)
  })

  it('names a planned restart immediately on epoch-change (close-1012) even before poll fires', () => {
    mockDaemon(true, true)
    render(<DaemonRestartBanner />)
    expect(screen.getByRole('status')).toHaveTextContent(/restarting the session daemon/i)
    expect(screen.getByRole('status')).toHaveTextContent(/back in a moment/i)
  })

  it('the spinner yields to prefers-reduced-motion', () => {
    mockDaemon(false, false)
    const { container } = render(<DaemonRestartBanner />)
    expect(container.querySelector('.animate-spin')).toHaveClass('motion-reduce:animate-none')
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
