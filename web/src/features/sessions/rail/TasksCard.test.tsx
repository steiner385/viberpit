import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { EMPTY_BACKGROUND_WORK_VIEW, type BackgroundWorkView, type SubAgent } from '@/session/selectors'

vi.mock('@/api/hooks', () => ({ useStopSubAgent: () => ({ mutate: vi.fn(), isPending: false }) }))

import { TasksCard } from './TasksCard'

function Wrapper({ children }: { children: ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

const agent: SubAgent = { toolId: 'a1', type: 'Explore', description: 'find the thing', status: 'running' }
const bg: BackgroundWorkView = {
  rows: [{ toolUseId: 'tu1', kind: 'shell', description: 'pytest -q', status: 'running', live: true }],
  wakeup: false, wakeupAt: null,
}

describe('TasksCard', () => {
  it('is a labelled "Tasks" section', () => {
    render(<TasksCard sessionId="s1" subAgentsSupported subAgents={[]} backgroundWork={EMPTY_BACKGROUND_WORK_VIEW} />, { wrapper: Wrapper })
    expect(screen.getByRole('region', { name: 'Tasks' })).toBeInTheDocument()
  })

  it('lists sub-agents when the engine supports them', () => {
    render(<TasksCard sessionId="s1" subAgentsSupported subAgents={[agent]} backgroundWork={EMPTY_BACKGROUND_WORK_VIEW} />, { wrapper: Wrapper })
    expect(screen.getByRole('list', { name: 'Sub-agents' })).toHaveTextContent('find the thing')
  })

  it('hides sub-agents without the capability', () => {
    render(<TasksCard sessionId="s1" subAgentsSupported={false} subAgents={[agent]} backgroundWork={EMPTY_BACKGROUND_WORK_VIEW} />, { wrapper: Wrapper })
    expect(screen.queryByRole('list', { name: 'Sub-agents' })).toBeNull()
    expect(screen.getByText('No background tasks')).toBeInTheDocument()
  })

  it('renders background rows inline', () => {
    render(<TasksCard sessionId="s1" subAgentsSupported subAgents={[]} backgroundWork={bg} />, { wrapper: Wrapper })
    expect(screen.getByRole('list', { name: 'Background work' })).toHaveTextContent('pytest -q')
    expect(screen.queryByText('No background tasks')).toBeNull()
  })

  it('renders an armed wakeup as background work', () => {
    render(<TasksCard sessionId="s1" subAgentsSupported subAgents={[]} backgroundWork={{ rows: [], wakeup: true, wakeupAt: null }} />, { wrapper: Wrapper })
    expect(screen.getByRole('list', { name: 'Background work' })).toHaveTextContent('wakeup')
  })

  it('with neither, shows "No background tasks"', () => {
    render(<TasksCard sessionId="s1" subAgentsSupported subAgents={[]} backgroundWork={EMPTY_BACKGROUND_WORK_VIEW} />, { wrapper: Wrapper })
    expect(screen.getByText('No background tasks')).toBeInTheDocument()
    expect(screen.queryByRole('list')).toBeNull()
  })
})
