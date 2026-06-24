import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { SubAgentList } from './SubAgentList'
import { agentDuration } from './subAgentDuration'
import type { SubAgent } from '@/session/selectors'

afterEach(cleanup)

const base: SubAgent = { toolId: 'x', type: 'Explore', description: 'find it', status: 'running' }

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
  afterEach(() => vi.useRealTimers())

  it('renders a frozen runtime for finished agents', () => {
    render(<SubAgentList subAgents={[{ ...base, status: 'done', startedAt: 1000, endedAt: 121_000 }]} />)
    expect(screen.getByText('2m 0s')).toBeInTheDocument()
    expect(screen.getByText('done')).toBeInTheDocument()
  })

  it('renders the live elapsed for a running agent', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(31_000))
    render(<SubAgentList subAgents={[{ ...base, status: 'running', startedAt: 1000 }]} />)
    expect(screen.getByText('30s')).toBeInTheDocument()
  })
})
