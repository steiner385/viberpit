// The one form for what a human owns on an initiative: its title and where it
// sits on the map. The primary picks the product; the touched set is the rest
// of THAT product's tree, so a primary from another product empties it.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Initiative } from '@/api/sdlc'
import type { ProductNode } from '@/api/roadmap'
import { EditInitiativeDialog } from './EditInitiativeDialog'

const h = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false }))
vi.mock('@/api/hooks', () => ({
  useUpdateInitiative: () => ({ mutate: h.mutate, isPending: h.isPending, isError: false, error: null }),
}))

const cap = (id: string, slug: string, title: string, children: ProductNode['capabilities'] = []) => ({
  id, slug, title, state: 'live', summary: null, docPath: null, commitSha: null, prNumber: null,
  parentSlug: null, tier: 'idle' as const, counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 },
  links: [], initiatives: [], children,
})
const product = (slug: string, name: string, capabilities: ProductNode['capabilities']): ProductNode => ({
  slug, name, tier: 'idle', counts: { inFlight: 0, blocked: 0, needsYou: 0, capabilities: capabilities.length, awaitingTriage: 0, needsHome: 0 },
  capabilities, initiatives: [],
})
const products = [
  product('kindash', 'KinDash', [cap('f1', 'task-views', 'Task views', [cap('f2', 'filters', 'Filters')])]),
  product('web', 'Web', [cap('f9', 'billing', 'Billing')]),
]
const initiative = {
  slug: 'views', title: 'Use-case-first task views', repo: 'o/r', stage: 'build', status: 'active',
  origin: 'human', originRef: null, epicRepo: null, epicNumber: null, createdAt: null, updatedAt: null,
  artifacts: [], epic: null, projectSlug: 'kindash',
  primaryCapability: { id: 'f1', slug: 'task-views', title: 'Task views' },
  capabilities: [
    { id: 'f1', slug: 'task-views', title: 'Task views', isPrimary: true },
    { id: 'f2', slug: 'filters', title: 'Filters', isPrimary: false },
  ],
} as unknown as Initiative

describe('EditInitiativeDialog', () => {
  beforeEach(() => { h.mutate.mockReset() })

  it('is seeded from the initiative and Save is disabled until something changes', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    expect(screen.getByLabelText('Title', { exact: false })).toHaveValue('Use-case-first task views')
    expect(screen.getByLabelText('Primary capability')).toHaveValue('f1')
    expect(screen.getByRole('checkbox', { name: 'Filters' })).toBeChecked()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('offers no way to un-home the initiative', () => {
    // "— none —" sent `primaryCapabilityId: ""` with an empty set, which cleared
    // the product and took the initiative off every node of the map.
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    expect(screen.queryByRole('option', { name: '— none —' })).toBeNull()
    // The only empty-valued option is the disabled placeholder.
    const empties = screen.getAllByRole('option').filter((o) => (o as HTMLOptionElement).value === '')
    expect(empties.map((o) => (o as HTMLOptionElement).disabled)).toEqual([true])
  })

  it('an initiative with no home yet shows the placeholder, not a blank select', () => {
    const homeless = { ...initiative, projectSlug: null, primaryCapability: null, capabilities: [] } as unknown as Initiative
    render(<EditInitiativeDialog initiative={homeless} products={products} open onClose={() => {}} />)
    const select = screen.getByLabelText('Primary capability', { exact: false }) as HTMLSelectElement
    expect(select.value).toBe('')
    expect(select.selectedOptions[0]).toHaveTextContent('— choose a capability —')
  })

  it('a same-product primary swap keeps the outgoing primary in the touched set', () => {
    // The initiative still touches what it was primary on; dropping it silently
    // would unlink a capability nobody asked to unlink.
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Primary capability'), { target: { value: 'f2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    const [vars] = h.mutate.mock.calls[0] as [{ slug: string; input: { primaryCapabilityId?: string; capabilityIds?: string[] } }]
    expect(vars.input.primaryCapabilityId).toBe('f2')
    expect(vars.input.capabilityIds).toEqual(expect.arrayContaining(['f1', 'f2']))
    expect(vars.input.capabilityIds?.[0]).toBe('f2')   // the primary leads the set
  })

  it('sends only the changed fields', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Title', { exact: false }), { target: { value: 'Task views, use-case first' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.mutate).toHaveBeenCalledWith(
      { slug: 'views', input: { title: 'Task views, use-case first' } }, expect.anything())
  })

  it('a touched change sends the whole set with the primary in it', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    fireEvent.click(screen.getByRole('checkbox', { name: 'Filters' }))   // untick
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.mutate).toHaveBeenCalledWith(
      { slug: 'views', input: { capabilityIds: ['f1'] } }, expect.anything())
  })

  it('a primary from another product empties the touched set and sends both', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Primary capability'), { target: { value: 'f9' } })
    expect(screen.queryByRole('checkbox', { name: 'Filters' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.mutate).toHaveBeenCalledWith(
      { slug: 'views', input: { primaryCapabilityId: 'f9', capabilityIds: ['f9'] } }, expect.anything())
  })

  it('a blank title disables Save and says why', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Title', { exact: false }), { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.getByText('Give it a title')).toBeInTheDocument()
  })
})
