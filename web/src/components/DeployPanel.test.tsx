// web/src/components/DeployPanel.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { DeployPanel } from './DeployPanel'
import * as hooks from '@/api/hooks'
import type { PendingDeploy, DeployProgress } from '@/api/types'

function mockHooks(
  pending: Partial<PendingDeploy>,
  deploy: Partial<DeployProgress>,
  mutate = vi.fn(),
) {
  vi.spyOn(hooks, 'usePendingDeploy').mockReturnValue(
    { data: pending } as unknown as ReturnType<typeof hooks.usePendingDeploy>)
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue(
    { data: deploy } as unknown as ReturnType<typeof hooks.useDeployStatus>)
  vi.spyOn(hooks, 'useStartDeploy').mockReturnValue(
    { mutate, isPending: false } as unknown as ReturnType<typeof hooks.useStartDeploy>)
}

describe('DeployPanel', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders nothing when up to date and idle', () => {
    mockHooks({ behind: false, commits: [], ci: 'green' }, { running: false, phases: {} })
    const { container } = render(<DeployPanel />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows pending count and enables Deploy when CI green', () => {
    const mutate = vi.fn()
    mockHooks(
      { behind: true, commits: [{ sha: 'abc1234', subject: 'feat x', author: 't', iso: 'i' }], ci: 'green' },
      { running: false, phases: {} }, mutate)
    render(<DeployPanel />)
    expect(screen.getByText(/1 change.*pending/i)).toBeInTheDocument()
    const btn = screen.getByRole('button', { name: /deploy now/i })
    expect(btn).toBeEnabled()
    fireEvent.click(btn)
    expect(mutate).toHaveBeenCalled()
  })

  it('disables Deploy with reason when CI not green', () => {
    mockHooks({ behind: true, commits: [{ sha: 'a', subject: 's', author: 't', iso: 'i' }], ci: 'pending' },
              { running: false, phases: {} })
    render(<DeployPanel />)
    expect(screen.getByRole('button', { name: /deploy now/i })).toBeDisabled()
    expect(screen.getByText(/CI pending/i)).toBeInTheDocument()
  })

  it('renders the phase checklist while running', () => {
    mockHooks({ behind: true, commits: [], ci: 'green' },
              { running: true, phases: { save: 'done', update: 'running' }, warnings: [] })
    render(<DeployPanel />)
    expect(screen.getByText(/update/i)).toBeInTheDocument()
  })

  it('flags sessions that failed to resume', () => {
    mockHooks({ behind: false, commits: [], ci: 'green' },
              { running: false, phases: { resume: 'done' }, warnings: [],
                restore: { restored: ['s1'], failed: ['s2', 's3'] } })
    render(<DeployPanel />)
    expect(screen.getByText(/2 session.*failed to resume/i)).toBeInTheDocument()
  })
  it('shows preflight phase when it fails', () => {
    mockHooks({ behind: true, commits: [], ci: 'green' },
              { running: false, phases: { preflight: 'failed' }, warnings: [],
                error: 'CI verdict=red' })
    render(<DeployPanel />)
    expect(screen.getByText(/preflight/i)).toBeInTheDocument()
  })

})
