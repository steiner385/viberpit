import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionMeta } from '../SessionMeta'
import type { SubAgent } from '@/session/selectors'

const baseProps = {
  account: 'work-max',
  model: 'claude-sonnet-4-5',
  contextPct: 78,
  quota: {
    sessionPct: 42,
    weeklyPct: 18,
    sessionResetsAt: '2026-06-19T14:30:00.000Z',
    weeklyResetsAt: null,
  },
  subAgents: [] as SubAgent[],
}

describe('SessionMeta', () => {
  it('shows the current account and model', () => {
    render(<SessionMeta {...baseProps} />)
    expect(screen.getByText('work-max')).toBeInTheDocument()
    expect(screen.getByText('claude-sonnet-4-5')).toBeInTheDocument()
  })

  it('shows remaining context percent', () => {
    render(<SessionMeta {...baseProps} />)
    expect(screen.getByLabelText(/remaining context/i)).toHaveTextContent('78%')
  })

  it('renders an em-dash for unknown model and context', () => {
    render(<SessionMeta {...baseProps} model={null} contextPct={null} />)
    expect(screen.getByLabelText(/current model/i)).toHaveTextContent('—')
    expect(screen.getByLabelText(/remaining context/i)).toHaveTextContent('—')
  })

  it('shows account quota when present', () => {
    render(<SessionMeta {...baseProps} />)
    const quota = screen.getByLabelText(/account quota/i)
    expect(quota).toHaveTextContent('42%')
    expect(quota).toHaveTextContent('18%')
  })

  it('labels each quota window and shows its reset time in a tooltip', () => {
    render(<SessionMeta {...baseProps} />)
    // The session window has a reset time → tooltip names the window + reset.
    const sess = screen.getByText(/sess 42%/i)
    expect(sess.getAttribute('title')).toMatch(/session limit/i)
    expect(sess.getAttribute('title')).toMatch(/resets/i)
    expect(sess.getAttribute('title')).toMatch(/jun/i)
    // The weekly window has no active reset → tooltip says so.
    const wk = screen.getByText(/wk 18%/i)
    expect(wk.getAttribute('title')).toMatch(/weekly limit/i)
    expect(wk.getAttribute('title')).toMatch(/no active window/i)
  })

  it('omits the quota segment when quota is null', () => {
    render(<SessionMeta {...baseProps} quota={null} />)
    expect(screen.queryByLabelText(/account quota/i)).toBeNull()
  })

  it('omits the sub-agents control when there are none', () => {
    render(<SessionMeta {...baseProps} subAgents={[]} />)
    expect(screen.queryByRole('button', { name: /sub-agent/i })).toBeNull()
  })

  it('toggles the sub-agent list open on click', () => {
    const subAgents: SubAgent[] = [
      { toolId: 'a', type: 'Explore', description: 'find it', status: 'running' },
      { toolId: 'b', type: 'Plan', description: 'plan it', status: 'done' },
    ]
    render(<SessionMeta {...baseProps} subAgents={subAgents} />)
    const toggle = screen.getByRole('button', { name: /sub-agent/i })
    expect(toggle).toHaveTextContent('2')
    // collapsed: list not shown
    expect(screen.queryByText('find it')).toBeNull()
    fireEvent.click(toggle)
    expect(screen.getByText('Explore')).toBeInTheDocument()
    expect(screen.getByText('Plan')).toBeInTheDocument()
  })
})
