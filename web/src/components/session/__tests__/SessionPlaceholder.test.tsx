import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { SessionPlaceholder } from '../SessionPlaceholder'

/**
 * SessionPlaceholder fills the transcript area when there are no turns to show,
 * disambiguating the three states that previously all rendered as a blank pane:
 * still connecting, still loading (replaying), and genuinely empty.
 */
describe('SessionPlaceholder', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows a "Connecting" message while the socket is connecting', () => {
    render(
      <SessionPlaceholder connectionState="connecting" replaying hasError={false} />,
    )
    expect(screen.getByTestId('session-placeholder')).toHaveAttribute('data-phase', 'connecting')
    expect(screen.getByTestId('session-placeholder').textContent).toMatch(/connecting/i)
  })

  it('shows a "Reconnecting" message when disconnected/reconnecting', () => {
    render(
      <SessionPlaceholder connectionState="reconnecting" replaying hasError={false} />,
    )
    expect(screen.getByTestId('session-placeholder')).toHaveAttribute('data-phase', 'connecting')
    expect(screen.getByTestId('session-placeholder').textContent).toMatch(/reconnecting/i)
  })

  it('shows a "Loading session" message while replaying over a live connection', () => {
    render(
      <SessionPlaceholder connectionState="connected" replaying hasError={false} />,
    )
    expect(screen.getByTestId('session-placeholder')).toHaveAttribute('data-phase', 'loading')
    expect(screen.getByTestId('session-placeholder').textContent).toMatch(/loading/i)
  })

  it('shows an empty-state message once loaded with no turns and no error', () => {
    render(
      <SessionPlaceholder connectionState="connected" replaying={false} hasError={false} />,
    )
    expect(screen.getByTestId('session-placeholder')).toHaveAttribute('data-phase', 'empty')
    expect(screen.getByTestId('session-placeholder').textContent).toMatch(/no messages/i)
  })

  it('defers to the error banner when loaded with an error', () => {
    render(
      <SessionPlaceholder connectionState="connected" replaying={false} hasError />,
    )
    expect(screen.getByTestId('session-placeholder')).toHaveAttribute('data-phase', 'error')
    // Does not duplicate the message — points the user at the banner above.
    expect(screen.getByTestId('session-placeholder').textContent).toMatch(/above/i)
  })

  it('surfaces a "taking longer than usual" hint after a delay while loading', () => {
    vi.useFakeTimers()
    render(
      <SessionPlaceholder connectionState="connected" replaying hasError={false} />,
    )
    expect(screen.queryByTestId('session-placeholder-slow')).toBeNull()
    act(() => { vi.advanceTimersByTime(8_000) })
    expect(screen.getByTestId('session-placeholder-slow')).toBeInTheDocument()
  })

  it('does not show the slow hint in the empty state', () => {
    vi.useFakeTimers()
    render(
      <SessionPlaceholder connectionState="connected" replaying={false} hasError={false} />,
    )
    act(() => { vi.advanceTimersByTime(20_000) })
    expect(screen.queryByTestId('session-placeholder-slow')).toBeNull()
  })
})
