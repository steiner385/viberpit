// The product Overview's Automation card: every schema field through the
// shared ConfigInput, disabled + a note until the product is live, and a
// sparse revision-checked patch on change (product automation policy phase 1).
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
  // The grouping test needs a field the earlier flat schema mock never carried
  // (task 9): without it, "every unmapped key falls into Capacity" reads as
  // true only because every key already IS a Capacity key.
  { key: 'capacity_mode', label: 'In-flight cap', type: 'enum', default: 'auto', help: '', choices: ['auto', 'pinned'] },
  { key: 'weight', label: 'Weight', type: 'int', default: 1, help: '', choices: null },
  { key: 'ladder_top', label: 'Ladder top tier', type: 'enum', default: 'fable', help: '', choices: ['haiku', 'sonnet', 'opus', 'fable'] },
  { key: 'default_model', label: 'Session model', type: 'enum', default: 'auto', help: '', choices: ['auto', 'sonnet', 'opus'] },
  { key: 'respect_dependencies', label: 'Respect issue dependencies', type: 'bool', default: true, help: '', choices: null },
  { key: 'skip_labels', label: 'Labels to skip', type: 'string-list', default: [], help: '', choices: null },
  { key: 'risk_tolerance', label: 'Risk tolerance', type: 'enum', default: 'cautious', help: '', choices: ['cautious', 'balanced', 'bold'] },
] as const

const GATES = {
  gates: [
    { id: 'artifact.merge', label: 'Merge the stage artifact PR', risk: 'low', action: 'Arm GitHub auto-merge on the PR.', reverse: 'Revert the docs commit', autoResolvesUnder: { cautious: false, balanced: true, bold: true } },
    { id: 'materialization.accept', label: 'Accept a plan decomposition', risk: 'medium', action: 'Create the epic and sub-issues.', reverse: 'Close the created issues', autoResolvesUnder: { cautious: false, balanced: false, bold: true } },
    { id: 'dispatch', label: 'Dispatch a ready issue to a coding session', risk: null, action: "Governed by the product's dispatch setting.", reverse: 'Archive the session', autoResolvesUnder: { cautious: false, balanced: false, bold: false } },
    { id: 'deploy.production', label: 'Deploy to production', risk: 'high', action: 'Never automatic.', reverse: 'Roll back', autoResolvesUnder: { cautious: false, balanced: false, bold: false } },
  ],
  tolerances: ['cautious', 'balanced', 'bold'],
  autoresolveEnabled: true,
}

const h = vi.hoisted(() => ({
  implementation: { data: undefined as unknown, isPending: false, isError: false },
  coverage: { data: undefined as unknown, isPending: false, isError: false },
  schema: { data: undefined as unknown, isLoading: false, isError: false },
  gates: { data: undefined as unknown, isPending: false, isError: false },
  update: vi.fn(),
  updateState: { isPending: false, isError: false, error: null as unknown },
}))
vi.mock('@/api/hooks', () => ({
  usePolicySchema: () => h.schema,
  useProductImplementationPolicy: () => h.implementation,
  useProductPolicyCoverage: () => h.coverage,
  useUpdateProductPolicy: () => ({ mutate: h.update, ...h.updateState }),
  useGithubRepos: () => ({ data: [], isLoading: false }),
  useSdlcGates: () => h.gates,
}))

const product = { slug: 'kindash', name: 'KinDash' } as ProductNode

function makeProject(over: Partial<Project> = {}): Project {
  return {
    id: 'p', slug: 'kindash', name: 'KinDash', description: null, status: null, portfolioTags: [],
    onboardingState: 'live', createdAt: null, updatedAt: null, repos: [], edges: [],
    featureCount: 0, workItemCount: 0, automationPolicy: null, automationPolicyUpdatedAt: null,
    priorityRank: null,
    ...over,
  }
}

beforeEach(() => {
  h.update.mockReset()
  Object.assign(h.implementation, { data: undefined, isPending: false, isError: false })
  Object.assign(h.coverage, { data: undefined, isPending: false, isError: false })
  Object.assign(h.schema, { data: SCHEMA, isLoading: false, isError: false })
  Object.assign(h.updateState, { isPending: false, isError: false, error: null })
  Object.assign(h.gates, { data: GATES, isPending: false, isError: false })
})

// Every render needs Router context now: the Capacity fieldset always links
// to the real capacity page (task 9), not only the repository-coverage
// sub-section that used to be the only `<Link>` in this component.
function renderCard(ui: Parameters<typeof render>[0]) {
  return render(<MemoryRouter>{ui}</MemoryRouter>)
}

/** Which of the three grouped `<fieldset>`s (by its `<legend>` text) an input
 *  lives in — the DOM-level proof that grouping, not just presence, worked. */
function legendFor(input: HTMLElement): string | null {
  return input.closest('fieldset')?.querySelector('legend')?.textContent ?? null
}

describe('AutomationPolicyCard', () => {
  it('renders every schema field with the default and the not-opted-in note when the policy is null', () => {
    renderCard(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: null })} />)
    expect(screen.getByText(/Issue dispatch is off until you explicitly change Dispatch from off/)).toBeInTheDocument()
    expect(screen.getByLabelText('Dispatch')).toHaveValue('off')
    expect(screen.getByLabelText('Max in flight')).toHaveValue(1)
    expect(screen.getByLabelText('Weight')).toHaveValue(1)
    expect(screen.getByLabelText('Ladder top tier')).toHaveValue('fable')
    expect(screen.getByLabelText('Session model')).toHaveValue('auto')
    expect(screen.getByLabelText('Respect issue dependencies')).toBeChecked()
    // dispatch listed first
    const labels = screen.getAllByText(/Dispatch|Minimum board status|Max in flight|Weight|Ladder top tier|Session model|Respect issue dependencies/)
    expect(labels[0]).toHaveTextContent('Dispatch')
  })

  it('adding a skip label sends the full list, additive to whatever is already there', () => {
    renderCard(<AutomationPolicyCard product={product} project={makeProject({
      automationPolicy: { skip_labels: ['renovate'] },
    })} />)
    const input = screen.getByLabelText('Labels to skip')
    fireEvent.change(input, { target: { value: 'on-hold' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(h.update).toHaveBeenCalledWith({
      policy: { skip_labels: ['renovate', 'on-hold'] }, expectedUpdatedAt: null,
    })
  })

  it('a change sends only the edited key and the observed revision', () => {
    renderCard(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: null })} />)
    fireEvent.change(screen.getByLabelText('Dispatch'), { target: { value: 'auto' } })
    expect(h.update).toHaveBeenCalledWith({ policy: { dispatch: 'auto' }, expectedUpdatedAt: null })
  })

  it('reads existing policy values over the defaults', () => {
    renderCard(<AutomationPolicyCard product={product} project={makeProject({
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
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Could not load the automation settings.')).toBeInTheDocument()
    expect(screen.queryByLabelText('Dispatch')).toBeNull()
  })

  it('a rejected save is bannered rather than springing the control back silently', () => {
    Object.assign(h.updateState, {
      isError: true,
      error: new ApiRequestError(422, { detail: { message: 'max_in_flight must be an int' } }),
    })
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('max_in_flight must be an int')).toBeInTheDocument()
    // …and the fields stay on screen so the value can be corrected in place.
    expect(screen.getByLabelText('Dispatch')).toBeInTheDocument()
  })

  it('disables every field and shows the not-live note until the product is live', () => {
    renderCard(<AutomationPolicyCard product={product} project={makeProject({ onboardingState: 'map_merged' })} />)
    expect(screen.getByText('Automation settings can be edited once the product is live.')).toBeInTheDocument()
    expect(screen.getByLabelText('Dispatch')).toBeDisabled()
    expect(screen.getByLabelText('Max in flight')).toBeDisabled()
    expect(screen.getByLabelText('Respect issue dependencies')).toBeDisabled()
  })

  it('lists every gate with its verdict under the configured tolerance', () => {
    renderCard(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: { risk_tolerance: 'balanced' } })} />)
    expect(screen.getByRole('heading', { name: 'Gates under balanced' })).toBeInTheDocument()
    const rows = screen.getAllByRole('row').slice(1)
    expect(rows).toHaveLength(4)
    expect(rows[0]).toHaveTextContent('Merge the stage artifact PR')
    expect(rows[0]).toHaveTextContent('Arm GitHub auto-merge on the PR.')
    expect(rows[0]).toHaveTextContent('Automatic')
    expect(rows[1]).toHaveTextContent('Waits for you')
    expect(rows[2]).toHaveTextContent('Policy-owned')
    expect(rows[3]).toHaveTextContent('Waits for you')
    // A policy-owned gate carries no risk level; the cell says which, never a
    // bare em dash (the house rule against em dashes in shipped copy).
    expect(rows[2]).toHaveTextContent('policy')
    for (const row of rows) expect(row).not.toHaveTextContent('—')
    expect(screen.queryByText(/switched off/)).not.toBeInTheDocument()
  })

  it('defaults to cautious and marks nothing automatic', () => {
    renderCard(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: null })} />)
    expect(screen.getByRole('heading', { name: 'Gates under cautious' })).toBeInTheDocument()
    expect(screen.queryByText('Automatic')).not.toBeInTheDocument()
  })

  it('says every gate waits when the kill switch is off, whatever the tolerance', () => {
    Object.assign(h.gates, { data: { ...GATES, autoresolveEnabled: false } })
    renderCard(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: { risk_tolerance: 'bold' } })} />)
    expect(screen.getByText(/Auto-resolve is switched off for this deployment/)).toBeInTheDocument()
    expect(screen.queryByText('Automatic')).not.toBeInTheDocument()
  })

  it('reports a gate list that will not load', () => {
    Object.assign(h.gates, { data: undefined, isPending: false, isError: true })
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Gate list is unavailable.')).toBeInTheDocument()
  })

  // Findability (spec D9): the flat field list used to bury the capacity
  // dials among model and gate settings with no way back to the real capacity
  // page. Grouping is verified at the DOM level (which fieldset's legend an
  // input sits under), not just "the legend text exists somewhere".
  it('groups fields into Capacity / Models / Gates fieldsets, in that order', () => {
    const { container } = renderCard(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: null })} />)
    // Only the three grouped inner fieldsets carry a `<legend>` — the outer
    // disabled fieldset that gates every control at once does not.
    const legends = Array.from(container.querySelectorAll('legend')).map((el) => el.textContent)
    expect(legends).toEqual(['Capacity', 'Models', 'Gates'])
    expect(legendFor(screen.getByLabelText('Dispatch'))).toBe('Capacity')
    expect(legendFor(screen.getByLabelText('Max in flight'))).toBe('Capacity')
    expect(legendFor(screen.getByLabelText('In-flight cap'))).toBe('Capacity')
    expect(legendFor(screen.getByLabelText('Weight'))).toBe('Capacity')
    expect(legendFor(screen.getByLabelText('Minimum board status'))).toBe('Capacity')
    expect(legendFor(screen.getByLabelText('Respect issue dependencies'))).toBe('Capacity')
    expect(legendFor(screen.getByLabelText('Labels to skip'))).toBe('Capacity')
    expect(legendFor(screen.getByLabelText('Ladder top tier'))).toBe('Models')
    expect(legendFor(screen.getByLabelText('Session model'))).toBe('Models')
    expect(legendFor(screen.getByLabelText('Risk tolerance'))).toBe('Gates')
  })

  it('links the Capacity group to the fleet capacity page', () => {
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    const link = screen.getByRole('link', { name: "See the fleet's capacity" })
    expect(link).toHaveAttribute('href', '/delivery?scope=capacity')
    expect(legendFor(link)).toBe('Capacity')
  })

  it('places a field the grouping lists do not recognize into Capacity rather than dropping it', () => {
    Object.assign(h.schema, { data: [
      ...SCHEMA,
      { key: 'not_yet_categorized', label: 'Not yet categorized', type: 'bool', default: false, help: '', choices: null },
    ] })
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(legendFor(screen.getByLabelText('Not yet categorized'))).toBe('Capacity')
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
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
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
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Repository coverage is unavailable.')).toBeInTheDocument()
    expect(screen.getByLabelText('Dispatch')).toBeEnabled()
  })

  it('identifies loading and empty registration states', () => {
    Object.assign(h.coverage, { isPending: true })
    const view = renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Checking repository coverage…')).toBeInTheDocument()
    Object.assign(h.coverage, { isPending: false, data: {
      projectSlug: 'kindash', observedAt: '2026-09-14T12:00:00Z', scope: 'repository-coverage',
      explanation: 'Registration diagnostics only.', repositories: [],
    } })
    view.rerender(<MemoryRouter><AutomationPolicyCard product={product} project={makeProject()} /></MemoryRouter>)
    expect(screen.getByText('No repositories are registered for this product.')).toBeInTheDocument()
  })
})

describe('implementation eligibility', () => {
  function policyPreview() {
    const permitted = {
      state: 'permitted', configuredDispatch: 'propose', policySource: 'product_policy',
      productId: 'p', productSlug: 'kindash', canonicalRepo: 'org/repo', approvalRequired: false,
      observedAt: '2026-09-15T12:00:00Z', policyRevision: 'revision',
      remainingChecks: ['target_ownership', 'engine'], note: 'Target checks still apply; this preview is not an authorization token.',
      constraints: [{ code: 'capacity_available', state: 'permitted', text: 'Successful capacity detail.', link: null }],
    }
    return {
      projectSlug: 'kindash', observedAt: '2026-09-15T12:00:00Z',
      scope: 'implementation-policy-eligibility', explanation: 'Implementation policy eligibility only.',
      repositories: [{
        requestedRepo: 'org/repo', canonicalRepo: 'org/repo', coverageState: 'unique', decisions: {
          automaticImplementation: {
            ...permitted, state: 'refused', approvalRequired: true,
            constraints: [{ code: 'approval_required', state: 'refused', text: 'The product requires approval.', link: '/roadmap/kindash' }],
          },
          automaticProposal: permitted,
          manualImplementation: { ...permitted, state: 'unknown', constraints: [
            { code: 'capacity_unknown', state: 'unknown', text: 'Capacity could not be established.', link: null },
          ] },
        },
      }],
    }
  }

  it('renders server eligibility and blocking reasons without inferring permission from settings', () => {
    h.implementation.data = policyPreview()
    renderCard(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: { dispatch: 'auto' } })} />)
    expect(screen.getByText('Automatic implementation: refused')).toBeInTheDocument()
    expect(screen.getByText('Automatic proposals: permitted')).toBeInTheDocument()
    expect(screen.getByText('Manual implementation: unknown')).toBeInTheDocument()
    expect(screen.getByText('Approval required before implementation.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'The product requires approval.' })).toHaveAttribute('href', '/roadmap/kindash')
    expect(screen.getByText('Capacity could not be established.')).toBeInTheDocument()
    expect(screen.queryByText('Successful capacity detail.')).not.toBeInTheDocument()
    expect(screen.getAllByText('Target checks still apply; this preview is not an authorization token.')).toHaveLength(1)
    expect(screen.getByText(/Manual implementation is a one-time request/)).toBeInTheDocument()
    expect(h.update).not.toHaveBeenCalled()
  })

  it.each([true, false])('keeps settings available when implementation policy is unavailable (error %s)', (isError) => {
    Object.assign(h.implementation, { isError })
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Implementation policy is unavailable.')).toBeInTheDocument()
    expect(screen.getByLabelText('Dispatch')).toBeEnabled()
    expect(screen.getByText('Repository coverage is unavailable.')).toBeInTheDocument()
  })

  it('shows pending and empty decisions explicitly', () => {
    h.implementation.isPending = true
    const view = renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Checking implementation policy…')).toBeInTheDocument()
    Object.assign(h.implementation, { isPending: false, data: { ...policyPreview(), repositories: [] } })
    view.rerender(<MemoryRouter><AutomationPolicyCard product={product} project={makeProject()} /></MemoryRouter>)
    expect(screen.getByText('No repository implementation decisions are available.')).toBeInTheDocument()
  })
})


it('disables all settings while a save is pending', () => {
  h.updateState.isPending = true
  renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
  expect(screen.getByLabelText('Dispatch')).toBeDisabled()
  expect(screen.getByLabelText('Max in flight')).toBeDisabled()
  expect(screen.getByLabelText('Respect issue dependencies')).toBeDisabled()
})

it('preserves the exact revision without rounding microseconds', () => {
  const revision = '2026-09-17T01:00:00.123456+00:00'
  renderCard(<AutomationPolicyCard product={product} project={makeProject({ automationPolicyUpdatedAt: revision })} />)
  fireEvent.change(screen.getByLabelText('Weight'), { target: { value: '4' } })
  expect(h.update).toHaveBeenCalledWith({ policy: { weight: 4 }, expectedUpdatedAt: revision })
})
