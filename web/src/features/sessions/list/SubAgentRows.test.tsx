import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SubAgentRows } from './SubAgentRows'
import type { SubAgentSummary } from '@/api/types'
import { STATUS_TONE } from '@/features/sessions/subAgentStatus'

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
    expect(screen.getByTestId('loc').textContent).toBe('/sessions/s1/agents/A')
  })

  it('renders each row status dot in the matching tone (#1562)', () => {
    // SubAgentSummary (the persisted, replay-derived shape this component renders)
    // has no 'stopped' status — that is a live-only concept the transcript summary
    // never emits (server/sessions/subagents.py derives only running/done/failed
    // from tool-call/result pairing). 'failed', unlike 'done'/'running' above, had
    // no dot-colour assertion at all.
    const failed: SubAgentSummary[] = [
      { agentId: 'C', parentAgentId: null, agentType: 'Explore', description: 'halted', status: 'failed' },
    ]
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <SubAgentRows sessionId="s1" subAgents={failed} />
      </MemoryRouter>,
    )
    const row = screen.getByLabelText(/Review sub-agent Explore: halted/)
    const dot = row.querySelector('span[aria-hidden]')
    expect(dot).not.toBeNull()
    for (const cls of STATUS_TONE.failed.split(' ')) {
      expect(dot).toHaveClass(cls)
    }
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
