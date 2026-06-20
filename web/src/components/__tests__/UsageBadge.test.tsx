import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { UsageBadge } from '../UsageBadge'

describe('UsageBadge', () => {
  it('renders the numeric percentage text', () => {
    render(<UsageBadge pct={73} label="session" />)
    expect(screen.getByText('73%')).toBeInTheDocument()
  })

  it('has a descriptive aria-label with label prop', () => {
    render(<UsageBadge pct={50} label="weekly" />)
    const badge = screen.getByLabelText('weekly usage: 50%')
    expect(badge).toBeInTheDocument()
  })

  it('has a descriptive aria-label without label prop', () => {
    render(<UsageBadge pct={80} />)
    const badge = screen.getByLabelText('Usage: 80%')
    expect(badge).toBeInTheDocument()
  })

  it('renders 0% correctly', () => {
    render(<UsageBadge pct={0} label="session" />)
    expect(screen.getByText('0%')).toBeInTheDocument()
    expect(screen.getByLabelText('session usage: 0%')).toBeInTheDocument()
  })

  it('renders 100% correctly', () => {
    render(<UsageBadge pct={100} label="session" />)
    expect(screen.getByText('100%')).toBeInTheDocument()
    expect(screen.getByLabelText('session usage: 100%')).toBeInTheDocument()
  })

  it('clamps values above 100', () => {
    render(<UsageBadge pct={150} />)
    expect(screen.getByText('100%')).toBeInTheDocument()
  })

  it('clamps values below 0', () => {
    render(<UsageBadge pct={-10} />)
    expect(screen.getByText('0%')).toBeInTheDocument()
  })
})
