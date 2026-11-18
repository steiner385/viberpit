// The product's repository management panel: how a retired repo reads, which
// actions each row offers, and what each action sends — in particular that
// retiring the primary cannot be submitted without naming a new ACTIVE primary,
// and that make-primary/retire carry the primary the panel DISPLAYED.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import type { Project, ProjectRepoInfo } from '@/api/pm'
import { ProductReposPanel } from './ProductReposPanel'

const mutation = () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null as unknown, reset: vi.fn() })
const hooks = vi.hoisted(() => ({
  add: null as unknown as ReturnType<typeof mutation>,
  primary: null as unknown as ReturnType<typeof mutation>,
  retire: null as unknown as ReturnType<typeof mutation>,
  unretire: null as unknown as ReturnType<typeof mutation>,
}))

vi.mock('@/api/hooks', () => ({
  useAddProjectRepo: () => hooks.add,
  useSetPrimaryRepo: () => hooks.primary,
  useRetireRepo: () => hooks.retire,
  useUnretireRepo: () => hooks.unretire,
  // The "Moved issues…" dialog, once opened.
  useMovedRefs: () => ({ data: { pairs: [] }, isLoading: false, isError: false }),
  useSuggestMovedRefs: () => ({ data: { pairs: [] }, isLoading: false, isError: false }),
  useRecordMovedRefs: () => mutation(),
}))

const repo = (r: string, extra: Partial<ProjectRepoInfo> = {}): ProjectRepoInfo => ({
  id: r, repo: r, isPrimary: false, backfillDone: true, lastSyncedAt: null,
  retiredAt: null, retiredReason: null, supersededBy: null, ...extra,
})

const project = (repos: ProjectRepoInfo[]): Project => ({
  id: 'p', slug: 'ghostdriver', name: 'Ghostdriver', description: null, status: null,
  portfolioTags: [], onboardingState: 'live', createdAt: null, updatedAt: null,
  repos, edges: [], featureCount: 0, workItemCount: 0, automationPolicy: null,
  automationPolicyUpdatedAt: null, priorityRank: null,
})

const THREE = project([
  repo('s/ghostdriver-mcp', { isPrimary: true }),
  repo('s/ghostdriver'),
  repo('s/ghostdriver-old', {
    retiredAt: '2026-11-01T00:00:00Z', retiredReason: 'consolidated', supersededBy: 's/ghostdriver',
  }),
])

const row = (name: string) =>
  screen.getAllByRole('listitem').find((li) => li.textContent?.includes(name) && within(li).queryByText(name))!

beforeEach(() => {
  hooks.add = mutation()
  hooks.primary = mutation()
  hooks.retire = mutation()
  hooks.unretire = mutation()
})

describe('ProductReposPanel', () => {
  it('renders a retired repo muted with its reason and "→ superseded", and only Un-retire', () => {
    render(<ProductReposPanel project={THREE} />)
    const retired = row('s/ghostdriver-old')
    expect(retired).toHaveTextContent('Retired · consolidated · → s/ghostdriver')
    expect(retired.className).toMatch(/text-ink-muted/)
    expect(within(retired).getByRole('button', { name: 'Un-retire s/ghostdriver-old' })).toBeInTheDocument()
    expect(within(retired).queryByRole('button', { name: /Make .* primary/ })).toBeNull()
    expect(within(retired).queryByRole('button', { name: /^Retire/ })).toBeNull()
  })

  it('badges the primary and offers Make primary only on other active repos', () => {
    render(<ProductReposPanel project={THREE} />)
    const primary = row('s/ghostdriver-mcp')
    expect(within(primary).getByText('primary')).toBeInTheDocument()
    expect(within(primary).queryByRole('button', { name: /Make .* primary/ })).toBeNull()
    expect(within(row('s/ghostdriver')).getByRole('button', { name: 'Make s/ghostdriver primary' })).toBeInTheDocument()
  })

  it('Make primary sends the displayed primary as expectedPrimary', () => {
    render(<ProductReposPanel project={THREE} />)
    fireEvent.click(screen.getByRole('button', { name: 'Make s/ghostdriver primary' }))
    expect(hooks.primary.mutate.mock.calls[0]![0]).toEqual({
      repo: 's/ghostdriver', expectedPrimary: 's/ghostdriver-mcp',
    })
  })

  it('Un-retire posts the repo', () => {
    render(<ProductReposPanel project={THREE} />)
    fireEvent.click(screen.getByRole('button', { name: 'Un-retire s/ghostdriver-old' }))
    expect(hooks.unretire.mutate.mock.calls[0]![0]).toBe('s/ghostdriver-old')
  })

  it('retiring the primary requires choosing a new primary from the ACTIVE repos', () => {
    render(<ProductReposPanel project={THREE} />)
    fireEvent.click(screen.getByRole('button', { name: 'Retire s/ghostdriver-mcp' }))
    const dialog = screen.getByRole('dialog', { name: 'Retire s/ghostdriver-mcp' })
    const submit = within(dialog).getByRole('button', { name: 'Retire' })
    expect(submit).toBeDisabled()
    const newPrimary = within(dialog).getByLabelText(/New primary/)
    const options = within(newPrimary).getAllByRole('option').map((o) => o.getAttribute('value'))
    // The retired repo and the repo being retired are not candidates.
    expect(options.filter(Boolean)).toEqual(['s/ghostdriver'])
    fireEvent.change(newPrimary, { target: { value: 's/ghostdriver' } })
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'consolidated' } })
    fireEvent.change(within(dialog).getByLabelText(/Superseded by/), { target: { value: 's/ghostdriver' } })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    expect(hooks.retire.mutate.mock.calls[0]![0]).toEqual({
      repo: 's/ghostdriver-mcp', reason: 'consolidated', supersededBy: 's/ghostdriver',
      newPrimary: 's/ghostdriver', expectedPrimary: 's/ghostdriver-mcp',
    })
  })

  it('retiring a non-primary repo asks for no new primary', () => {
    render(<ProductReposPanel project={THREE} />)
    fireEvent.click(screen.getByRole('button', { name: 'Retire s/ghostdriver' }))
    const dialog = screen.getByRole('dialog', { name: 'Retire s/ghostdriver' })
    expect(within(dialog).queryByLabelText(/New primary/)).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retire' }))
    expect(hooks.retire.mutate.mock.calls[0]![0]).toEqual({
      repo: 's/ghostdriver', reason: null, supersededBy: null, newPrimary: null,
      expectedPrimary: 's/ghostdriver-mcp',
    })
  })

  it('cannot retire the last active repo', () => {
    render(<ProductReposPanel project={project([repo('s/only', { isPrimary: true })])} />)
    expect(screen.getByRole('button', { name: 'Retire s/only' })).toBeDisabled()
  })

  it('adds a repository, refusing a malformed one', () => {
    render(<ProductReposPanel project={THREE} />)
    const input = screen.getByLabelText(/Add repository/)
    const add = screen.getByRole('button', { name: 'Add' })
    fireEvent.change(input, { target: { value: 'not-a-repo' } })
    expect(add).toBeDisabled()
    expect(screen.getByText(/Use owner\/repo/)).toBeInTheDocument()
    fireEvent.change(input, { target: { value: ' s/ghostdriver-web ' } })
    fireEvent.click(add)
    expect(hooks.add.mutate.mock.calls[0]![0]).toEqual({ repo: 's/ghostdriver-web' })
  })

  it('shows the server message when an action is refused', async () => {
    const { ApiRequestError } = await import('@/api/client')
    hooks.primary.isError = true
    hooks.primary.error = new ApiRequestError(409, {
      detail: { error: 'primary_changed', message: 'The primary repo is now s/x, not s/ghostdriver-mcp; reload and retry.' },
    })
    render(<ProductReposPanel project={THREE} />)
    expect(screen.getByRole('alert')).toHaveTextContent('The primary repo is now s/x')
  })

  it('opens the Moved issues dialog', async () => {
    render(<ProductReposPanel project={THREE} />)
    fireEvent.click(screen.getByRole('button', { name: 'Moved issues…' }))
    expect(await screen.findByRole('dialog', { name: 'Moved issues' })).toBeInTheDocument()
  })
})
