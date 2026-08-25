// web/src/components/DeployOverlay.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { DeployOverlay } from './DeployOverlay'
import * as hooks from '@/api/hooks'
import * as controller from '@/lib/deployController'
import type { DeployProgress, PendingDeploy } from '@/api/types'

function mock(opts: {
  ctrl: Partial<controller.DeployControllerValue>
  deploy?: Partial<DeployProgress>
  pending?: Partial<PendingDeploy>
  isError?: boolean
  failureCount?: number
}) {
  vi.spyOn(controller, 'useDeployController').mockReturnValue({
    intent: 'idle', visible: false, starting: false, startError: null,
    start: vi.fn(), dismiss: vi.fn(), ...opts.ctrl,
  })
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue({
    data: opts.deploy, isError: opts.isError ?? false, failureCount: opts.failureCount ?? 0,
  } as unknown as ReturnType<typeof hooks.useDeployStatus>)
  vi.spyOn(hooks, 'usePendingDeploy').mockReturnValue({
    data: opts.pending ?? { commits: [], targetSha: 'abcdef0' },
  } as unknown as ReturnType<typeof hooks.usePendingDeploy>)
}

describe('DeployOverlay', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders nothing when not visible', () => {
    mock({ ctrl: { visible: false } })
    const { container } = render(<DeployOverlay />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the stepper while running with the active phase', () => {
    mock({
      ctrl: { visible: true, intent: 'idle' },
      deploy: { running: true, phases: { preflight: 'done', save: 'running' }, warnings: [] },
      pending: { commits: [{ sha: 'a', subject: 's', author: 't', iso: 'i' }], targetSha: 'deadbee' },
    })
    render(<DeployOverlay />)
    expect(screen.getByText(/upgrading/i)).toBeInTheDocument()
    expect(screen.getByText(/backing up live sessions/i)).toBeInTheDocument()
  })

  it('shows reconnecting (not an error) when the poll fails mid-run', () => {
    mock({
      ctrl: { visible: true, intent: 'idle' },
      deploy: { running: true, phases: { restart: 'running' }, warnings: [] },
      isError: true, failureCount: 2,
    })
    render(<DeployOverlay />)
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument()
  })

  it('surfaces the error message in the running overlay when a phase failed (issue #741)', () => {
    mock({
      ctrl: { visible: true, intent: 'idle' },
      deploy: {
        running: true,
        phases: { preflight: 'done', save: 'done', update: 'done', report: 'done', restart: 'failed' },
        error: 'restart: health gate failed',
        warnings: [],
      },
    })
    render(<DeployOverlay />)
    expect(screen.getByText(/restart: health gate failed/i)).toBeInTheDocument()
  })

  it('shows a held failure with the error and a Close button', () => {
    const dismiss = vi.fn()
    mock({
      ctrl: { visible: true, intent: 'held-failure', dismiss },
      deploy: { running: false, phases: { update: 'failed' }, error: 'fast-forward failed', warnings: [],
                restore: { restored: [], failed: ['s2'] } },
    })
    render(<DeployOverlay />)
    expect(screen.getByText(/fast-forward failed/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /close/i }))
    expect(dismiss).toHaveBeenCalled()
  })

  it('shows the success confirmation', () => {
    mock({ ctrl: { visible: true, intent: 'held-success' }, deploy: { running: false, phases: {}, warnings: [] } })
    render(<DeployOverlay />)
    expect(screen.getByText(/deployed/i)).toBeInTheDocument()
  })

  it('surfaces lost sessions as a warning on the success screen', () => {
    mock({
      ctrl: { visible: true, intent: 'held-success' },
      deploy: { running: false, phases: {}, warnings: [], restore: { restored: ['s1'], failed: ['s2'] } },
    })
    render(<DeployOverlay />)
    expect(screen.getByText(/deployed/i)).toBeInTheDocument()
    expect(screen.getByText(/couldn.t be resumed/i)).toBeInTheDocument()
    expect(screen.getByText(/s2/)).toBeInTheDocument()
  })
})
