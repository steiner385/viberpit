// The product Overview's Automation card: every schema field through the
// shared ConfigInput, disabled + a note until the product is live, and a
// whole-object-replace patch on change (product automation policy phase 1).
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import type { Project } from '@/api/pm'
import type { ProductNode } from '@/api/roadmap'
import { AutomationPolicyCard } from './AutomationPolicyCard'

const SCHEMA = [
  { key: 'dispatch', label: 'Dispatch', type: 'enum', default: 'off', help: 'Whether the runner works this board.', choices: ['off', 'propose', 'auto'] },
  { key: 'queue_min_status', label: 'Minimum board status', type: 'enum', default: 'ready', help: '', choices: ['ready', 'triaged'] },
  { key: 'max_in_flight', label: 'Max in flight', type: 'int', default: 1, help: '', choices: null },
  { key: 'weight', label: 'Weight', type: 'int', default: 1, help: '', choices: null },
  { key: 'max_attempts', label: 'Max attempts per issue', type: 'int', default: 2, help: '', choices: null },
  { key: 'default_model', label: 'Session model', type: 'enum', default: 'auto', help: '', choices: ['auto', 'sonnet', 'opus'] },
  { key: 'respect_dependencies', label: 'Respect issue dependencies', type: 'bool', default: true, help: '', choices: null },
] as const

const h = vi.hoisted(() => ({
  coverage: { data: undefined as unknown, isPending: false, isError: false },
  schema: { data: undefined as unknown, isLoading: false, isError: false },
  update: vi.fn(),
  updateState: { isError: false, error: null as unknown },
}))
vi.mock('@/api/hooks', () => ({
  usePolicySchema: () => h.schema,
  useProductPolicyCoverage: () => h.coverage,
  useUpdateProductPolicy: () => ({ mutate: h.update, isPending: false, ...h.updateState }),
  useGithubRepos: () => ({ data: [], isLoading: false }),
}))

const product = { slug: 'kindash', name: 'KinDash' } as ProductNode

function makeProject(over: Partial<Project> = {}): Project {
  return {
    id: 'p', slug: 'kindash', name: 'KinDash', description: null, status: null, portfolioTags: [],
    onboardingState: 'live', createdAt: null, updatedAt: null, repos: [], edges: [],
    featureCount: 0, workItemCount: 0, automationPolicy: null,
    ...over,
  }
}

beforeEach(() => {
  h.update.mockReset()
  Object.assign(h.coverage, { data: undefined, isPending: false, isError: false })
  Object.assign(h.schema, { data: SCHEMA, isLoading: false, isError: false })
  Object.assign(h.updateState, { isError: false, error: null })
})

describe('AutomationPolicyCard', () => {
  it('renders every schema field with the default and the not-opted-in note when the policy is null', () => {
    render(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: null })} />)
    expect(screen.getByText(/Issue dispatch is off until you explicitly change Dispatch from off/)).toBeInTheDocument()
    expect(screen.getByLabelText('Dispatch')).toHaveValue('off')
    expect(screen.getByLabelText('Max in flight')).toHaveValue(1)
    expect(screen.getByLabelText('Weight')).toHaveValue(1)
    expect(screen.getByLabelText('Max attempts per issue')).toHaveValue(2)
    expect(screen.getByLabelText('Session model')).toHaveValue('auto')
    expect(screen.getByLabelText('Respect issue dependencies')).toBeChecked()
    // dispatch listed first
    const labels = screen.getAllByText(/Dispatch|Minimum board status|Max in flight|Weight|Max attempts per issue|Session model|Respect issue dependencies/)
    expect(labels[0]).toHaveTextContent('Dispatch')
  })

  it('a change to dispatch sends the full effective blob with dispatch: auto', () => {
    render(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: null })} />)
    fireEvent.change(screen.getByLabelText('Dispatch'), { target: { value: 'auto' } })
    expect(h.update).toHaveBeenCalledWith({
      dispatch: 'auto',
      queue_min_status: 'ready',
      max_in_flight: 1,
      weight: 1,
      max_attempts: 2,
      default_model: 'auto',
      respect_dependencies: true,
    })
  })

  it('reads existing policy values over the defaults', () => {
    render(<AutomationPolicyCard product={product} project={makeProject({
      automationPolicy: { dispatch: 'propose', max_in_flight: 3 },
    })} />)
    expect(screen.queryByText(/Issue dispatch is off until you explicitly change Dispatch from off/)).toBeNull()
    expect(screen.getByLabelText('Dispatch')).toHaveValue('propose')
    expect(screen.getByLabelText('Max in flight')).toHaveValue(3)
    // Unset keys still fall back to the schema default.
    expect(screen.getByLabelText('Weight')).toHaveValue(1)
  })

  it('a failed schema fetch banners instead of skeletoning forever', () => {
    Object.assign(h.schema, { data: undefined, isLoading: false, isError: true })
    render(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Could not load the automation settings.')).toBeInTheDocument()
    expect(screen.queryByLabelText('Dispatch')).toBeNull()
  })

  it('a rejected save is bannered rather than springing the control back silently', () => {
    Object.assign(h.updateState, {
      isError: true,
      error: new ApiRequestError(422, { detail: { message: 'max_in_flight must be an int' } }),
    })
    render(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('max_in_flight must be an int')).toBeInTheDocument()
    // …and the fields stay on screen so the value can be corrected in place.
    expect(screen.getByLabelText('Dispatch')).toBeInTheDocument()
  })

  it('disables every field and shows the not-live note until the product is live', () => {
    render(<AutomationPolicyCard product={product} project={makeProject({ onboardingState: 'map_merged' })} />)
    expect(screen.getByText('Automation settings can be edited once the product is live.')).toBeInTheDocument()
    expect(screen.getByLabelText('Dispatch')).toBeDisabled()
    expect(screen.getByLabelText('Max in flight')).toBeDisabled()
    expect(screen.getByLabelText('Respect issue dependencies')).toBeDisabled()
  })
})


describe('repository coverage', () => {
  it('renders server evidence and product links without treating coverage as permission', () => {
    h.coverage.data = {
      projectSlug: 'kindash', observedAt: '2026-09-14T12:00:00Z', scope: 'repository-coverage',
      explanation: 'Registration diagnostics only; this does not authorize automation.',
      repositories: [
        { requestedRepo: 'org/old', canonicalRepo: 'org/current', state: 'unique',
          owner: { id: 'p', slug: 'kindash' }, candidates: [{ id: 'p', slug: 'kindash' }],
          unresolvedRepos: [], reason: 'One registered product owns this repository.' },
        { requestedRepo: 'org/shared', canonicalRepo: 'org/shared', state: 'ambiguous',
          owner: null, candidates: [{ id: 'q', slug: 'other product' }],
          unresolvedRepos: [], reason: 'Multiple products register the same repository.' },
        { requestedRepo: 'org/unknown', canonicalRepo: null, state: 'unresolved',
          owner: null, candidates: [], unresolvedRepos: ['org/private'],
          reason: 'Could not verify a registered repository.' },
        { requestedRepo: 'org/missing', canonicalRepo: 'org/missing', state: 'missing',
          owner: null, candidates: [], unresolvedRepos: [], reason: 'No registered product.' },
      ],
    }
    render(<MemoryRouter><AutomationPolicyCard product={product} project={makeProject()} /></MemoryRouter>)
    expect(screen.getByText('Registration diagnostics only; this does not authorize automation.')).toBeInTheDocument()
    expect(screen.getByText('One registered product owns this repository.')).toBeInTheDocument()
    expect(screen.getByText('Multiple products register the same repository.')).toBeInTheDocument()
    expect(screen.getByText('Could not verify a registered repository.')).toBeInTheDocument()
    expect(screen.getByText('No registered product.')).toBeInTheDocument()
    expect(screen.getByText(/org\/private/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'other product' })).toHaveAttribute('href', '/roadmap/other%20product')
    expect(screen.getByRole('link', { name: 'kindash' })).toHaveAttribute('href', '/roadmap/kindash')
    expect(h.update).not.toHaveBeenCalled()
  })

  it.each([true, false])('keeps policy controls when coverage is unavailable (error %s)', (isError) => {
    Object.assign(h.coverage, { isError })
    render(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Repository coverage is unavailable.')).toBeInTheDocument()
    expect(screen.getByLabelText('Dispatch')).toBeEnabled()
  })

  it('identifies loading and empty registration states', () => {
    Object.assign(h.coverage, { isPending: true })
    const view = render(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Checking repository coverage…')).toBeInTheDocument()
    Object.assign(h.coverage, { isPending: false, data: {
      projectSlug: 'kindash', observedAt: '2026-09-14T12:00:00Z', scope: 'repository-coverage',
      explanation: 'Registration diagnostics only.', repositories: [],
    } })
    view.rerender(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('No repositories are registered for this product.')).toBeInTheDocument()
  })
})
