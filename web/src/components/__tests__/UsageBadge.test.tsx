import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { UsageBadge, StatusPill } from '../UsageBadge'

describe('UsageBadge', () => {
  it('renders the numeric percentage text', () => {
    render(<UsageBadge pct={73} label="session" />)
    expect(screen.getByText('73%')).toBeInTheDocument()
  })

  it('has a descriptive aria-label with label prop', () => {
    render(<UsageBadge pct={50} label="weekly" />)
    const badge = screen.getByLabelText(/weekly usage: 50%/i)
    expect(badge).toBeInTheDocument()
  })

  it('has a descriptive aria-label without label prop', () => {
    render(<UsageBadge pct={80} />)
    const badge = screen.getByLabelText(/Usage: 80%/i)
    expect(badge).toBeInTheDocument()
  })

  it('renders 0% correctly', () => {
    render(<UsageBadge pct={0} label="session" />)
    expect(screen.getByText('0%')).toBeInTheDocument()
    expect(screen.getByLabelText(/session usage: 0%/i)).toBeInTheDocument()
  })

  it('renders 100% correctly', () => {
    render(<UsageBadge pct={100} label="session" />)
    expect(screen.getByText('100%')).toBeInTheDocument()
    expect(screen.getByLabelText(/session usage: 100%/i)).toBeInTheDocument()
  })

  it('clamps values above 100', () => {
    render(<UsageBadge pct={150} />)
    expect(screen.getByText('100%')).toBeInTheDocument()
  })

  it('clamps values below 0', () => {
    render(<UsageBadge pct={-10} />)
    expect(screen.getByText('0%')).toBeInTheDocument()
  })

  // ── window labeling + reset-time tooltip ──────────────────────────────────

  it('renders a visible label so session and weekly bars are distinguishable', () => {
    render(<UsageBadge pct={42} label="session" />)
    // The label is shown to sighted users, not just in the aria-label.
    expect(screen.getByText('session')).toBeInTheDocument()
  })

  it('exposes a reset time in the tooltip (title) when resetsAt is set', () => {
    render(
      <UsageBadge pct={70} label="weekly" resetsAt="2026-06-19T14:30:00.000Z" />,
    )
    const badge = screen.getByLabelText(/weekly usage: 70%/i)
    // Intl always emits month + day regardless of locale → "Jun" / "19".
    expect(badge.getAttribute('title')).toMatch(/resets/i)
    expect(badge.getAttribute('title')).toMatch(/jun/i)
    // The reset time is also surfaced to screen readers via the aria-label.
    expect(badge.getAttribute('aria-label')).toMatch(/resets/i)
  })

  it('says "no active window" in the tooltip when resetsAt is null', () => {
    render(<UsageBadge pct={0} label="session" resetsAt={null} />)
    const badge = screen.getByLabelText(/session usage: 0%/i)
    expect(badge.getAttribute('title')).toMatch(/no active window/i)
  })
})

describe('StatusPill — session status tiers', () => {
  it('renders complete as "Your turn" with neutral text', () => {
    const { container } = render(<StatusPill status="complete" />)
    expect(screen.getByText('Your turn')).toBeInTheDocument()
    expect(container.querySelector('.text-emerald-700')).toBeFalsy()
  })

  it('renders needs_attention as "Turn failed" with red text', () => {
    const { container } = render(<StatusPill status="needs_attention" />)
    expect(screen.getByText('Turn failed')).toBeInTheDocument()
    expect(container.querySelector('.text-red-700')).toBeTruthy()
  })

  it('renders done as "Your turn" with neutral (non-emerald) text', () => {
    const { container } = render(<StatusPill status="done" />)
    expect(screen.getByText('Your turn')).toBeInTheDocument()
    expect(container.querySelector('.text-emerald-700')).toBeFalsy()
  })
})
