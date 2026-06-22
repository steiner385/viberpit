import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'

// Mock the runtime hook to return a fixed transcript: a main turn + one sub-agent thread.
vi.mock('@/session/useSessionRuntime', () => ({
  useSessionRuntime: () => ({
    sessionId: 's1',
    title: 'Parent Session',
    mode: 'interactive',
    toolDetailMode: 'compact',
    thinkingCollapsed: false,
    subAgents: [{ toolId: 'toolu_A', type: 'Explore', description: 'd', status: 'running' }],
    state: {
      turns: [{ id: 'm1', role: 'assistant', blocks: [{ kind: 'text', id: 'm1:t0', text: 'MAIN_ONLY' }] }],
      subAgents: {
        toolu_A: { turns: [{ id: 's1', role: 'assistant', blocks: [{ kind: 'text', id: 's1:t0', text: 'SUBWORK' }] }], openBlockId: null },
      },
    },
  }),
}))

import { SubSessionView } from '../SubSessionView'

describe('SubSessionView', () => {
  it('renders the sub-agent thread, not the main turn, with a breadcrumb', () => {
    render(
      <MemoryRouter>
        <SubSessionView sessionId="s1" agentId="toolu_A" onBack={() => {}} />
      </MemoryRouter>,
    )
    expect(screen.getByText(/SUBWORK/)).toBeInTheDocument()
    expect(screen.queryByText(/MAIN_ONLY/)).toBeNull()
    expect(screen.getByText(/Parent Session/)).toBeInTheDocument()
    expect(screen.getByText(/Explore/)).toBeInTheDocument()
    expect(screen.getByText(/running…/)).toBeInTheDocument()
  })
})
