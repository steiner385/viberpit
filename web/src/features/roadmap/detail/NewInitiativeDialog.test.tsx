// The "+ Initiative" dialog: what it sends, where it goes, and what it says when
// the server refuses.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import type { CreateInitiativeInput } from '@/api/sdlc'
import { MAP } from '../__fixtures__/map'
import { NewInitiativeDialog } from './NewInitiativeDialog'

const create = vi.hoisted(() => ({
  mutate: vi.fn(),
  isPending: false,
  isError: false,
  error: null as unknown,
}))

// The project query is per-test state: "which repository?" has a different
// honest answer while it loads, when it fails, and when it really is empty.
const projectQuery = vi.hoisted(() => ({
  data: undefined as unknown,
  isLoading: false,
  isError: false,
}))

vi.mock('@/api/hooks', () => ({
  useCreateInitiative: () => create,
  useProject: () => projectQuery,
}))

const PROJECT = {
  id: 'p', slug: 'kindash', name: 'KinDash', description: null, status: null, portfolioTags: [],
  onboardingState: 'live', createdAt: null, updatedAt: null, edges: [], featureCount: 3, workItemCount: 0,
  automationPolicy: null,
  repos: [
    { id: 'r1', repo: 'cairnea/KinDash', isPrimary: true, backfillDone: true, lastSyncedAt: null },
    { id: 'r2', repo: 'cairnea/KinDash-infra', isPrimary: false, backfillDone: true, lastSyncedAt: null },
  ],
}

const product = MAP.products[0]!

function Here() {
  return <div data-testid="here">{useLocation().pathname}</div>
}

function mount(onClose = vi.fn()) {
  render(
    <MemoryRouter initialEntries={['/roadmap/kindash']}>
      <NewInitiativeDialog product={product} open onClose={onClose} />
      <Here />
    </MemoryRouter>,
  )
  return onClose
}

const submit = () => fireEvent.submit(screen.getByRole('button', { name: 'Create' }).closest('form')!)

beforeEach(() => {
  create.mutate = vi.fn()
  create.isPending = false
  create.isError = false
  create.error = null
  Object.assign(projectQuery, { data: PROJECT, isLoading: false, isError: false })
})

describe('NewInitiativeDialog', () => {
  it('derives the slug from the title and sends the primary capability', () => {
    const onClose = mount()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Use-case first views!' } })
    expect(screen.getByLabelText(/Slug/)).toHaveValue('use-case-first-views')
    fireEvent.change(screen.getByLabelText(/Primary capability/), { target: { value: 'f-task-views' } })
    submit()

    const [input, opts] = create.mutate.mock.calls[0] as [CreateInitiativeInput, { onSuccess: (i: { slug: string }) => void }]
    expect(input).toEqual({
      slug: 'use-case-first-views',
      title: 'Use-case first views!', // sent verbatim; only the slug is normalised
      repo: 'cairnea/KinDash', // the project's first repo, unchanged
      origin: 'human',
      primaryCapabilityId: 'f-task-views',
      capabilityIds: ['f-task-views'],
    })
    // The dialog closes and the new initiative's own page opens.
    act(() => opts.onSuccess({ slug: 'use-case-first-views' }))
    expect(onClose).toHaveBeenCalled()
    expect(screen.getByTestId('here')).toHaveTextContent('/initiatives/use-case-first-views')
  })

  it('keeps an edited slug and the chosen repo, and omits an unset capability', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Billing rework' } })
    fireEvent.change(screen.getByLabelText(/Slug/), { target: { value: 'billing v2' } })
    fireEvent.change(screen.getByLabelText(/Repository/), { target: { value: 'cairnea/KinDash-infra' } })
    submit()
    expect(create.mutate.mock.calls[0]![0]).toEqual({
      slug: 'billing-v2',
      title: 'Billing rework',
      repo: 'cairnea/KinDash-infra',
      origin: 'human',
    })
  })

  it('refuses to submit without a title', () => {
    mount()
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    submit()
    expect(create.mutate).not.toHaveBeenCalled()
  })

  it('says why Create is disabled when the slug normalises to nothing', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: '!!!' } })
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    expect(screen.getByText('Add a letter or number to the slug')).toBeInTheDocument()
    expect(screen.getByLabelText(/Slug/)).toHaveAttribute('aria-invalid', 'true')
  })

  it('does not blame the product for a repo list that is still loading or failed', () => {
    Object.assign(projectQuery, { data: undefined, isLoading: true, isError: false })
    const { unmount } = render(
      <MemoryRouter><NewInitiativeDialog product={product} open onClose={vi.fn()} /></MemoryRouter>,
    )
    expect(screen.getByLabelText(/Repository/)).toHaveTextContent('Loading repositories…')
    unmount()

    Object.assign(projectQuery, { data: undefined, isLoading: false, isError: true })
    render(<MemoryRouter><NewInitiativeDialog product={product} open onClose={vi.fn()} /></MemoryRouter>)
    expect(screen.getByLabelText(/Repository/)).toHaveTextContent('Could not load repositories')
    // Nothing to create against either way.
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
  })

  it('says a resolved product really has no repository', () => {
    Object.assign(projectQuery, { data: { ...PROJECT, repos: [] }, isLoading: false, isError: false })
    mount()
    expect(screen.getByLabelText(/Repository/)).toHaveTextContent('No repository is attached to this product')
  })

  it('shows the server message when the create fails', () => {
    create.isError = true
    create.error = new ApiRequestError(409, { detail: { message: 'Slug already exists' } })
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Slug already exists')
  })

  it('renders nothing when closed', () => {
    render(
      <MemoryRouter>
        <NewInitiativeDialog product={product} open={false} onClose={vi.fn()} />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
