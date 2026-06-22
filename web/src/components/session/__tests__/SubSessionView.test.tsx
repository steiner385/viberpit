import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'

// Mock the stream hook to return a fixed transcript with a sub-agent turn.
vi.mock('@/session/useSessionStream', () => ({
  useSessionStream: () => ({
    state: {
      turns: [
        { id: 'm1', role: 'assistant', agentId: null, blocks: [{ kind: 'text', id: 'm1:t0', text: 'MAIN' }] },
        { id: 's1', role: 'assistant', agentId: 'toolu_A', blocks: [{ kind: 'text', id: 's1:t0', text: 'SUBWORK' }] },
      ],
      subAgents: { toolu_A: { agentId: 'toolu_A', parentAgentId: null, agentType: 'Explore', description: 'd', status: 'running' } },
      replaying: false, status: { state: 'idle', count: 0 },
    },
    connectionState: 'open',
  }),
}))

import { SubSessionView } from '../SubSessionView'

describe('SubSessionView', () => {
  it('shows only the sub-agent transcript and a breadcrumb', () => {
    render(<SubSessionView sessionId="s1" agentId="toolu_A" parentTitle="Parent" onBack={vi.fn()} />)
    expect(screen.getByText(/SUBWORK/)).toBeInTheDocument()
    expect(screen.queryByText(/^MAIN$/)).toBeNull()
    expect(screen.getByText(/Parent/)).toBeInTheDocument()
    expect(screen.getByText(/Explore/)).toBeInTheDocument()
  })
})
