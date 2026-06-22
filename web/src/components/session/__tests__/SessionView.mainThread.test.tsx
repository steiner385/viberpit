import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { TranscriptView } from '../TranscriptView'
import { selectTurnsForAgent } from '@/session/selectors'
import type { Turn } from '@/session/reducer'

// Unit-level proof that the main thread excludes sub-agent turns.
describe('main thread filtering', () => {
  it('renders main-agent text but not sub-agent text', () => {
    const turns: Turn[] = [
      { id: 'm1', role: 'assistant', agentId: null, blocks: [{ kind: 'text', id: 'm1:t0', text: 'MAIN_ONLY' }] },
      { id: 's1', role: 'assistant', agentId: 'toolu_A', blocks: [{ kind: 'text', id: 's1:t0', text: 'SUBAGENT_ONLY' }] },
    ]
    render(<TranscriptView turns={selectTurnsForAgent(turns, null)} permissionMode="interactive"
                           onDecision={() => {}} onAnswer={() => {}} />)
    expect(screen.getByText(/MAIN_ONLY/)).toBeInTheDocument()
    expect(screen.queryByText(/SUBAGENT_ONLY/)).toBeNull()
  })
})
