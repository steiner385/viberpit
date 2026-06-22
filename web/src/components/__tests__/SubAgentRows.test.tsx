import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SubAgentRows } from '../SubAgentRows'
import type { SubAgentSummary } from '@/api/types'

const subs: SubAgentSummary[] = [
  { agentId: 'A', parentAgentId: null, agentType: 'Explore', description: 'find X', status: 'done' },
  { agentId: 'B', parentAgentId: 'A', agentType: 'agent', description: 'nested', status: 'running' },
]

describe('SubAgentRows', () => {
  it('renders nested rows and fires onOpenSubAgent', () => {
    const onOpen = vi.fn()
    render(<SubAgentRows sessionId="s1" subAgents={subs} depth={0} onOpenSubAgent={onOpen} />)
    expect(screen.getByText(/find X/)).toBeInTheDocument()
    expect(screen.getByText(/nested/)).toBeInTheDocument()
    fireEvent.click(screen.getByText(/find X/))
    expect(onOpen).toHaveBeenCalledWith('s1', 'A')
  })
})
