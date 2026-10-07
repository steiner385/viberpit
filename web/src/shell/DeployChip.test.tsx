import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import type { PendingDeploy, DeployProgress } from '@/api/types'
import { deriveDeployState } from '@/lib/deployState'
import { DeployChip } from './DeployChip'

const state = vi.hoisted(() => ({ pending: {} as Partial<PendingDeploy>, deploy: {} as Partial<DeployProgress> }))
vi.mock('@/lib/deployState', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/deployState')>()
  return { ...actual, useDeployState: () => actual.deriveDeployState(state.pending as PendingDeploy, state.deploy as DeployProgress) }
})

function renderChip(showLabel = false) {
  return render(<MemoryRouter initialEntries={['/sessions?drawer=help']}><DeployChip showLabel={showLabel} /></MemoryRouter>)
}

beforeEach(() => {
  state.pending = {
    behind: true, ci: 'green', tipCi: 'pending', deployableSha: 'one', deployableCount: 1,
    commits: ['one', 'two', 'three'].map((sha) => ({ sha, subject: sha, author: 'Tony', iso: '' })),
  }
  state.deploy = { running: false }
})

describe('DeployChip', () => {
  it('shows the shared green-frontier fraction in the collapsed rail and preserves the drawer', () => {
    renderChip()
    const link = screen.getByRole('link', { name: /Deploy.*3 changes pending deploy.*1 deployable now.*Latest CI pending/ })
    expect(link).toHaveAttribute('href', '/settings/deploy?drawer=help')
    expect(link).not.toHaveAttribute('aria-current')
    expect(screen.getByText('1/3')).toBeVisible()
    expect(link).toHaveClass('bg-accent-wash')
    expect(deriveDeployState(state.pending as PendingDeploy, undefined).reason).toBe('1 of 3 deployable (tip CI pending)')
  })

  it('shows readiness and tip CI as visible text when expanded', () => {
    renderChip(true)
    expect(screen.getByText('Deploy · 1/3 ready')).toBeVisible()
    expect(screen.getByText('CI pending')).toBeVisible()
  })

  it.each([
    ['green', 3, '3/3'], ['pending', 0, '0/3'], ['unrun', 0, '0/3'], ['unavailable', 0, '0/3'],
  ])('shows %s readiness without inventing pending CI', (ci, deployableCount, fraction) => {
    state.pending = { ...state.pending, ci, tipCi: ci, deployableCount, deployableSha: ci === 'green' ? 'three' : '' }
    renderChip()
    expect(screen.getByText(fraction)).toBeVisible()
    if (ci !== 'green') expect(screen.getByRole('link')).not.toHaveClass('bg-accent-wash')
    if (ci === 'unrun') expect(screen.getByRole('link')).toHaveAccessibleName(/Latest CI never ran/)
  })

  it('treats a green tip without the newer count field as fully deployable', () => {
    state.pending = { ...state.pending, ci: 'green', tipCi: 'green', deployableCount: undefined }
    renderChip()
    expect(screen.getByText('3/3')).toBeVisible()
  })

  it('keeps large fractions exact and allows wrapping within the narrow rail', () => {
    state.pending = { ...state.pending, deployableCount: 102, commits: Array.from({ length: 123 }, (_, i) => ({ sha: String(i), subject: '', author: '', iso: '' })) }
    renderChip()
    expect(screen.getByText('102/123')).toHaveClass('max-w-full', 'break-all')
    expect(screen.getByRole('link')).toHaveAccessibleName(/123 changes pending deploy. 102 deployable now/)
  })

  it('shows the summary on hover and keyboard focus and dismisses it with Escape', async () => {
    renderChip()
    const user = userEvent.setup()
    const link = screen.getByRole('link')
    await user.hover(link)
    expect(screen.getByRole('tooltip', { hidden: true })).toHaveTextContent('1 deployable now')
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('tooltip', { hidden: true })).toBeNull()
    await user.unhover(link)
    await user.tab()
    expect(link).toHaveFocus()
    expect(screen.getByRole('tooltip', { hidden: true })).toHaveTextContent('Latest CI pending')
  })

  it('warns about red tip CI even when an older change is deployable', () => {
    state.pending.tipCi = 'red'
    renderChip(true)
    expect(screen.getByRole('link')).toHaveAccessibleName(/Latest CI red/)
    expect(screen.getByText('CI red').parentElement?.querySelector('svg')).toBeInTheDocument()
  })

  it('keeps deployment progress visible when the pending poll has caught up', () => {
    state.pending = { behind: false, commits: [] }
    state.deploy.running = true
    renderChip(true)
    const link = screen.getByRole('link', { name: /Deploy.*Deployment in progress/ })
    expect(screen.getByText('Deploying…')).toBeVisible()
    expect(link.querySelector('svg')).toHaveClass('motion-safe:animate-spin')
    expect(link).not.toHaveAccessibleName(/deployable now/)
  })

  it('keeps recovery failures reachable without pending changes', () => {
    state.pending = { behind: false, commits: [] }
    state.deploy = { running: false, restore: { failed: [], gaveUp: ['lost-session'] } }
    renderChip(true)
    expect(screen.getByRole('link')).toHaveAccessibleName(/Needs attention/)
    expect(screen.getByText('Needs attention')).toBeVisible()
  })

  it('is absent when current and healthy', () => {
    state.pending = { behind: false, commits: [] }
    renderChip()
    expect(screen.queryByRole('link')).toBeNull()
  })
})
