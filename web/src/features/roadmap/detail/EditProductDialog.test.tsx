// The rename/repoint dialog: what it pre-fills, what it sends (sparse —
// unchanged fields are omitted), where a slug change navigates, and what it
// says when the server refuses (409 conflict or the open-map-changes guard).
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import type { Project, UpdateProjectInput } from '@/api/pm'
import { EditProductDialog } from './EditProductDialog'

const update = vi.hoisted(() => ({
  mutate: vi.fn(),
  isPending: false,
  isError: false,
  error: null as unknown,
}))

vi.mock('@/api/hooks', () => ({ useUpdateProject: () => update }))

const PROJECT: Project = {
  id: 'p', slug: 'lakotagrades', name: 'LakotaGrades', description: null, status: null,
  portfolioTags: [], onboardingState: 'live', createdAt: null, updatedAt: null,
  repos: [{ id: 'r1', repo: 'steiner385/lakota-grades-mcp', isPrimary: true, backfillDone: true, lastSyncedAt: null }],
  edges: [], featureCount: 0, workItemCount: 0, automationPolicy: null, automationPolicyUpdatedAt: null,
}

function Here() {
  return <div data-testid="here">{useLocation().pathname}</div>
}

function mount(project = PROJECT, onClose = vi.fn()) {
  render(
    <MemoryRouter initialEntries={[`/roadmap/${project.slug}`]}>
      <EditProductDialog project={project} open onClose={onClose} />
      <Here />
    </MemoryRouter>,
  )
  return onClose
}

const submit = () => fireEvent.submit(screen.getByRole('button', { name: 'Save' }).closest('form')!)

beforeEach(() => {
  update.mutate = vi.fn()
  update.isPending = false
  update.isError = false
  update.error = null
})

describe('EditProductDialog', () => {
  it('pre-fills from the current product', () => {
    mount()
    expect(screen.getByLabelText(/Name/)).toHaveValue('LakotaGrades')
    expect(screen.getByLabelText(/Slug/)).toHaveValue('lakotagrades')
    expect(screen.getByLabelText(/Primary repository/)).toHaveValue('steiner385/lakota-grades-mcp')
  })

  it('sends a sparse patch — only the fields that actually changed', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'FridgeSheet' } })
    fireEvent.change(screen.getByLabelText(/Slug/), { target: { value: 'fridgesheet' } })
    fireEvent.change(screen.getByLabelText(/Primary repository/), { target: { value: 'steiner385/fridgesheet' } })
    submit()
    const [input] = update.mutate.mock.calls[0] as [UpdateProjectInput]
    expect(input).toEqual({ name: 'FridgeSheet', slug: 'fridgesheet', repo: 'steiner385/fridgesheet' })
  })

  it('omits unchanged fields, and closes without mutating on a no-op submit', () => {
    const onClose = mount()
    submit()
    expect(update.mutate).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalled()
  })

  it('navigates to the new slug path on a slug rename, not the old one', () => {
    const onClose = mount()
    fireEvent.change(screen.getByLabelText(/Slug/), { target: { value: 'fridgesheet' } })
    submit()
    const [, opts] = update.mutate.mock.calls[0] as [
      UpdateProjectInput,
      { onSuccess: (p: Project) => void },
    ]
    act(() => opts.onSuccess({ ...PROJECT, slug: 'fridgesheet' }))
    expect(onClose).toHaveBeenCalled()
    expect(screen.getByTestId('here')).toHaveTextContent('/roadmap/fridgesheet')
  })

  it('stays put when the name-only save resolves with the slug unchanged', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'FridgeSheet' } })
    submit()
    const [, opts] = update.mutate.mock.calls[0] as [
      UpdateProjectInput,
      { onSuccess: (p: Project) => void },
    ]
    act(() => opts.onSuccess({ ...PROJECT, name: 'FridgeSheet' }))
    expect(screen.getByTestId('here')).toHaveTextContent('/roadmap/lakotagrades')
  })

  it('refuses a non-branch-safe slug, and says why', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Slug/), { target: { value: 'Not Safe!' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.getByText(/Lowercase letters, digits and hyphens only/)).toBeInTheDocument()
    submit()
    expect(update.mutate).not.toHaveBeenCalled()
  })

  it('refuses a repository that is not owner/repo, and says why', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Primary repository/), { target: { value: 'not-a-repo' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.getByText(/Use owner\/repo/)).toBeInTheDocument()
  })

  it('shows the server message when the update fails, e.g. the open-map-changes guard', () => {
    update.isError = true
    update.error = new ApiRequestError(409, {
      detail: { message: 'Merge or abandon the open map-changes PR before renaming the slug or repointing the primary repo.' },
    })
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Merge or abandon the open map-changes PR')
  })

  it('renders nothing when closed', () => {
    render(
      <MemoryRouter>
        <EditProductDialog project={PROJECT} open={false} onClose={vi.fn()} />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
