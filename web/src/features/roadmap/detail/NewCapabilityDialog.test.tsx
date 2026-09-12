// "+ Capability" / "+ Sub-capability": the form that opens the product's
// map-changes PR with a `create` op. It does NOT navigate to what it created
// (R8c-10) — the map is an index and only learns about the commit on the next
// sync pass, so the new capability's page would be a "Not found" for that whole
// window. It closes and says where the change went.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { toast } from 'sonner'
import { ApiRequestError } from '@/api/client'
import type { MapChangeInput } from '@/api/pm'
import { cap, product } from '../__fixtures__/map'
import { NewCapabilityDialog } from './NewCapabilityDialog'

const h = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  isPending: false,
  errorMessage: vi.fn((_e: unknown, fallback: string) => `mapped:${fallback}`),
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/api/hooks', () => ({
  useEditMapChanges: () => ({ mutateAsync: h.mutateAsync, isPending: h.isPending, isError: false, error: null }),
  mapChangesErrorMessage: (e: unknown, fallback: string) => h.errorMessage(e, fallback),
}))

const RESULT = { pointer: { prNumber: 31 }, path: 'docs/product/features/voice-notes.md', commitSha: 'sha1' }

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
  h.mutateAsync = vi.fn().mockResolvedValue(RESULT)
  h.isPending = false
  h.errorMessage.mockClear()
  vi.mocked(toast.success).mockClear()
})

describe('NewCapabilityDialog', () => {
  it('derives the slug from the title, sends the create, then closes and toasts without navigating', async () => {
    const onClose = mount()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Voice notes!' } })
    expect(screen.getByLabelText(/Slug/)).toHaveValue('voice-notes')
    create()
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(sent()).toEqual({ op: 'create', slug: 'voice-notes', title: 'Voice notes!', parentSlug: null })
    // R8c-10: the capability is not on the map yet, so its page would be a "Not
    // found" and a retry from there would 422 `slug_taken`. Stay put, say where
    // it went.
    expect(screen.getByTestId('here')).toHaveTextContent(/^\/roadmap\/kindash$/)
    expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('Added "Voice notes!"'))
    expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('PR #31'))
    expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('next sync'))
  })

  it('says the PR is still being opened when the pointer carries no number yet', async () => {
    h.mutateAsync = vi.fn().mockResolvedValue({ ...RESULT, pointer: { prNumber: null } })
    mount()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Voice notes' } })
    create()
    await waitFor(() => expect(toast.success).toHaveBeenCalled())
    expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('PR #…'))
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
    expect(toast.success).not.toHaveBeenCalled()
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
