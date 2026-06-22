import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SubAgentRows } from '../SubAgentRows'
import type { SubAgentSummary } from '@/api/types'

const subs: SubAgentSummary[] = [
  { agentId: 'A', parentAgentId: null, agentType: 'Explore', description: 'find X', status: 'done' },
  { agentId: 'B', parentAgentId: 'A', agentType: 'agent', description: 'nested', status: 'running' },
]

function LocationProbe() {
  return <span data-testid="loc">{useLocation().pathname}</span>
}

describe('SubAgentRows', () => {
  it('renders nested rows and navigates to the sub-agent route on click', () => {
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <SubAgentRows sessionId="s1" subAgents={subs} />
        <Routes>
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText(/find X/)).toBeInTheDocument()
    expect(screen.getByText(/nested/)).toBeInTheDocument()
    fireEvent.click(screen.getByText(/find X/))
    expect(screen.getByTestId('loc').textContent).toBe('/session/s1/subagent/A')
  })

  it('renders nothing when there are no sub-agents', () => {
    const { container } = render(
      <MemoryRouter>
        <SubAgentRows sessionId="s1" subAgents={[]} />
      </MemoryRouter>,
    )
    expect(container.querySelector('[aria-label="Sub-agents"]')).toBeNull()
  })
})
