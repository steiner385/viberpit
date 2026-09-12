import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, act, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SubAgentList } from './SubAgentList'
import { agentDuration } from './subAgentDuration'
import type { SubAgent } from '@/session/selectors'

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const real = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...real, useNavigate: () => navigate }
})

afterEach(cleanup)

const base: SubAgent = { toolId: 'x', type: 'Explore', description: 'find it', status: 'running' }

function renderList(subAgents: SubAgent[]) {
  return render(<MemoryRouter><SubAgentList sessionId="s1" subAgents={subAgents} /></MemoryRouter>)
}

describe('agentDuration', () => {
  it('returns live elapsed (now − start) for a running agent', () => {
    expect(agentDuration({ ...base, status: 'running', startedAt: 1000 }, 91_000)).toBe('1m 30s')
  })

  it('returns frozen total runtime for a finished agent', () => {
    expect(agentDuration({ ...base, status: 'done', startedAt: 1000, endedAt: 73_000 }, 999_999)).toBe('1m 12s')
  })

  it('returns null when start ts is missing', () => {
    expect(agentDuration({ ...base, status: 'running' }, 5000)).toBeNull()
  })

  it('returns null for a finished agent with no end ts', () => {
    expect(agentDuration({ ...base, status: 'done', startedAt: 1000 }, 5000)).toBeNull()
  })

  it('never shows a negative duration if start is slightly ahead of now', () => {
    expect(agentDuration({ ...base, status: 'running', startedAt: 5000 }, 4000)).toBe('0s')
  })
})

describe('SubAgentList', () => {
  afterEach(() => { vi.useRealTimers(); navigate.mockReset() })

  it('renders a frozen runtime for finished agents', () => {
    renderList([{ ...base, status: 'done', startedAt: 1000, endedAt: 121_000 }])
    expect(screen.getByText('2m 0s')).toBeInTheDocument()
    expect(screen.getByText('completed')).toBeInTheDocument()
  })

  it('renders the live elapsed for a running agent', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(31_000))
    renderList([{ ...base, status: 'running', startedAt: 1000 }])
    // Flush the shared useNow catch-up timer (setTimeout 0) so the hook ticks
    // from 0 → Date.now() (31_000) before we assert.
    act(() => { vi.advanceTimersByTime(0) })
    expect(screen.getByText('30s')).toBeInTheDocument()
  })

  it('shows the live activity instead of the description while running', () => {
    renderList([{ ...base, type: 'Explore', description: 'scan', status: 'running', activity: 'Running Bash' }])
    expect(screen.getByText('Running Bash')).toBeInTheDocument()
  })

  it('renders the stopped status label', () => {
    renderList([{ ...base, status: 'stopped' }])
    expect(screen.getByText('stopped')).toBeInTheDocument()
  })

  it('each row is a link into that sub-agent', () => {
    renderList([{ ...base, toolId: 'toolu_A', type: 'Explore', description: 'find it' }])
    const row = screen.getByRole('button', { name: /Open sub-agent Explore: find it/ })
    fireEvent.click(row)
    expect(navigate).toHaveBeenCalledWith(expect.stringContaining('/sessions/s1/agents/toolu_A'))
  })
})
