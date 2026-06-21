import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AdoptPanel } from '../AdoptPanel'
import * as hooks from '@/api/hooks'
import type { AdoptionCandidate } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useAdoptable: vi.fn(),
  useAdoptSession: vi.fn(),
}))

function cand(overrides: Partial<AdoptionCandidate> = {}): AdoptionCandidate {
  return {
    pid: 5, account: 'kindash', cwd: '/home/u/repo', claudeSessionId: 'aaa',
    name: 'Work', mapped: true, tracked: false, skipReason: null,
    lastActivity: new Date().toISOString(), pidAlive: true, ...overrides,
  }
}

const mutate = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  ;(hooks.useAdoptSession as ReturnType<typeof vi.fn>).mockReturnValue({ mutate, isPending: false })
})

function mockCandidates(cands: AdoptionCandidate[]) {
  ;(hooks.useAdoptable as ReturnType<typeof vi.fn>).mockReturnValue({ data: { candidates: cands } })
}

describe('AdoptPanel', () => {
  it('renders an adoptable candidate with name + account, keeps the UUID out of visible text', () => {
    mockCandidates([cand()])
    render(<AdoptPanel />)
    expect(screen.getByText('Work')).toBeInTheDocument()
    expect(screen.getByText(/kindash/)).toBeInTheDocument()
    expect(screen.queryByText('aaa')).not.toBeInTheDocument() // UUID demoted to title attr
  })

  it('shows the empty state when nothing is untracked', () => {
    mockCandidates([])
    render(<AdoptPanel />)
    expect(screen.getByText(/All running sessions are managed/i)).toBeInTheDocument()
  })

  it('requires the checkbox before confirming an active force-takeover', async () => {
    const user = userEvent.setup()
    mockCandidates([cand()])
    render(<AdoptPanel />)
    await user.click(screen.getByRole('button', { name: /stop & take over/i }))
    const confirm = within(screen.getByRole('dialog')).getByRole('button', { name: /stop & take over/i })
    expect(confirm).toBeDisabled()
    await user.click(screen.getByRole('checkbox'))
    expect(confirm).toBeEnabled()
    await user.click(confirm)
    await waitFor(() => expect(mutate).toHaveBeenCalledWith('aaa'))
  })

  it('groups unmapped candidates under "Not adoptable" with plain-language copy', () => {
    mockCandidates([cand({ mapped: false, account: null, skipReason: 'unmapped' })])
    render(<AdoptPanel />)
    expect(screen.getByText(/Not adoptable/i)).toBeInTheDocument()
    expect(screen.getByText(/Not a managed account/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /take over/i })).not.toBeInTheDocument()
  })
})
