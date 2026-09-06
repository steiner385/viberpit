import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NoticeCard } from '../NoticeCard'

describe('NoticeCard', () => {
  it('renders the title and body', () => {
    render(<NoticeCard notice={{ title: 'PR broke', body: 'acme/checkout CI is red' }} />)
    expect(screen.getByText('PR broke')).toBeInTheDocument()
    expect(screen.getByText('acme/checkout CI is red')).toBeInTheDocument()
  })

  it('shows a relative time computed from the injected clock', () => {
    const now = 1_000_000 + 2 * 60 * 60 * 1000
    render(<NoticeCard notice={{ title: 'x', body: '', ts: 1_000_000 }} now={now} />)
    expect(screen.getByText('2h ago')).toBeInTheDocument()
  })

  it('links to the notice url when present', () => {
    render(<NoticeCard notice={{ title: 'x', body: '', url: '/session/abc' }} />)
    expect(screen.getByRole('link', { name: 'View' })).toHaveAttribute('href', '/session/abc')
  })

  it('calls onAsk with the prefilled question, quoting the title', async () => {
    const onAsk = vi.fn()
    render(<NoticeCard notice={{ title: 'Backlog runner errored', body: '' }} onAsk={onAsk} />)
    await userEvent.click(screen.getByRole('button', { name: 'Ask about it' }))
    expect(onAsk).toHaveBeenCalledWith('About "Backlog runner errored": what happened and what should I do?')
  })

  it('hides the Ask about it button when onAsk is absent (read-only views)', () => {
    render(<NoticeCard notice={{ title: 'x', body: '' }} />)
    expect(screen.queryByRole('button', { name: 'Ask about it' })).toBeNull()
  })
})
