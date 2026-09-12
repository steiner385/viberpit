// "+ Capability" / "+ Sub-capability": the form that opens the product's
// map-changes PR with a `create` op, and then takes the user to the capability
// it just proposed.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import type { MapChangeInput } from '@/api/pm'
import { cap, product } from '../__fixtures__/map'
import { NewCapabilityDialog } from './NewCapabilityDialog'

const h = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  isPending: false,
  errorMessage: vi.fn((_e: unknown, fallback: string) => `mapped:${fallback}`),
}))
vi.mock('@/api/hooks', () => ({
  useEditMapChanges: () => ({ mutateAsync: h.mutateAsync, isPending: h.isPending, isError: false, error: null }),
  mapChangesErrorMessage: (e: unknown, fallback: string) => h.errorMessage(e, fallback),
}))

const kindash = product({
  slug: 'kindash', name: 'KinDash',
  capabilities: [cap({ slug: 'tasks', title: 'Tasks', children: [cap({ slug: 'task-views', title: 'Task views', parentSlug: 'tasks' })] })],
})

function Here() {
  return <div data-testid="here">{useLocation().pathname}</div>
}

function mount(over: { parentSlug?: string } = {}, onClose = vi.fn()) {
  render(
    <MemoryRouter initialEntries={['/roadmap/kindash']}>
      <NewCapabilityDialog product={kindash} open onClose={onClose} {...over} />
      <Here />
    </MemoryRouter>,
  )
  return onClose
}
const create = () => fireEvent.submit(screen.getByRole('button', { name: 'Create' }).closest('form')!)
const sent = () => h.mutateAsync.mock.calls[0]?.[0] as MapChangeInput | undefined

beforeEach(() => {
  h.mutateAsync = vi.fn().mockResolvedValue({})
  h.isPending = false
  h.errorMessage.mockClear()
})

describe('NewCapabilityDialog', () => {
  it('derives the slug from the title, sends the create, and opens the new capability', async () => {
    const onClose = mount()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Voice notes!' } })
    expect(screen.getByLabelText(/Slug/)).toHaveValue('voice-notes')
    create()
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(sent()).toEqual({ op: 'create', slug: 'voice-notes', title: 'Voice notes!', parentSlug: null })
    expect(screen.getByTestId('here')).toHaveTextContent('/roadmap/kindash/capabilities/voice-notes')
  })

  it('keeps an edited slug and sends the summary and the preset parent', async () => {
    mount({ parentSlug: 'tasks' })
    expect(screen.getByLabelText(/Parent capability/)).toHaveValue('tasks')
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Task filters' } })
    fireEvent.change(screen.getByLabelText(/Slug/), { target: { value: 'filters v2' } })
    fireEvent.change(screen.getByLabelText(/Summary/), { target: { value: '  Narrow a list down.  ' } })
    create()
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalled())
    expect(sent()).toEqual({
      op: 'create', slug: 'filters-v2', title: 'Task filters', parentSlug: 'tasks',
      summary: 'Narrow a list down.',
    })
  })

  it('refuses to submit without a title, and says why a punctuation title cannot be one', () => {
    mount()
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: '!!!' } })
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    expect(screen.getByText('Add a letter or number to the slug')).toBeInTheDocument()
    create()
    expect(h.mutateAsync).not.toHaveBeenCalled()
  })

  it('keeps the form open with the mapped error when the server refuses', async () => {
    const refusal = new ApiRequestError(422, { detail: { error: 'slug_taken', message: 'already on the branch' } })
    h.mutateAsync = vi.fn().mockRejectedValue(refusal)
    const onClose = mount()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Voice notes' } })
    create()
    expect(await screen.findByRole('alert')).toHaveTextContent('mapped:')
    expect(h.errorMessage).toHaveBeenCalledWith(refusal, expect.any(String))
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('here')).toHaveTextContent(/^\/roadmap\/kindash$/)
  })

  it('renders nothing when closed', () => {
    render(
      <MemoryRouter>
        <NewCapabilityDialog product={kindash} open={false} onClose={vi.fn()} />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
