// The one form for what a human owns on a capability: its title, where it sits
// in the tree, and its summary. Each is its own commit on the map-changes
// branch (R8c-1), so Save sends up to three requests in sequence and stops at
// the first one the server refuses.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ApiRequestError } from '@/api/client'
import type { MapChangeInput } from '@/api/pm'
import { cap, product } from '../__fixtures__/map'
import { EditCapabilityDialog } from './EditCapabilityDialog'

const h = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  isPending: false,
  // The component must ROUTE its message through the shared mapper rather than
  // writing its own copy; the mapper's own wording is pinned in its test.
  errorMessage: vi.fn((_e: unknown, fallback: string) => `mapped:${fallback}`),
}))
vi.mock('@/api/hooks', () => ({
  useEditMapChanges: () => ({ mutateAsync: h.mutateAsync, isPending: h.isPending, isError: false, error: null }),
  mapChangesErrorMessage: (e: unknown, fallback: string) => h.errorMessage(e, fallback),
}))

// tasks ▸ task-views ▸ filters, plus a sibling: enough tree for "not itself,
// not under itself" to have something to exclude AND something to offer.
const tasks = cap({
  slug: 'tasks', title: 'Tasks', summary: 'Everything a family does with a task.',
  children: [
    cap({ slug: 'task-views', title: 'Task views', parentSlug: 'tasks',
      children: [cap({ slug: 'filters', title: 'Filters', parentSlug: 'task-views' })] }),
  ],
})
const kindash = product({ slug: 'kindash', name: 'KinDash', capabilities: [cap({ slug: 'billing', title: 'Billing' }), tasks] })

const mount = (capability = tasks, onClose = vi.fn()) => {
  render(<EditCapabilityDialog product={kindash} capability={capability} open onClose={onClose} />)
  return onClose
}
const save = () => fireEvent.submit(screen.getByRole('button', { name: 'Save' }).closest('form')!)
const ops = () => h.mutateAsync.mock.calls.map(([input]) => input as MapChangeInput)

beforeEach(() => {
  h.mutateAsync = vi.fn().mockResolvedValue({})
  h.isPending = false
  h.errorMessage.mockClear()
})

describe('EditCapabilityDialog', () => {
  it('is seeded from the capability and Save is disabled until something changes', () => {
    mount()
    expect(screen.getByLabelText(/Title/)).toHaveValue('Tasks')
    expect(screen.getByLabelText(/Summary/)).toHaveValue('Everything a family does with a task.')
    expect(screen.getByLabelText(/Parent capability/)).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('sends only the changed field as its own op', async () => {
    const onClose = mount()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Task management' } })
    save()
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(ops()).toEqual([{ op: 'rename', slug: 'tasks', title: 'Task management' }])
  })

  it('sends rename, then reparent, then summary — one request each', async () => {
    const onClose = mount(cap({ slug: 'task-views', title: 'Task views', parentSlug: 'tasks', summary: 'Old words.' }))
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Views' } })
    fireEvent.change(screen.getByLabelText(/Parent capability/), { target: { value: 'billing' } })
    fireEvent.change(screen.getByLabelText(/Summary/), { target: { value: 'New words.' } })
    save()
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(ops()).toEqual([
      { op: 'rename', slug: 'task-views', title: 'Views' },
      { op: 'reparent', slug: 'task-views', parentSlug: 'billing' },
      { op: 'summary', slug: 'task-views', summary: 'New words.' },
    ])
  })

  it('moves a capability back to the root', async () => {
    const onClose = mount(cap({ slug: 'task-views', title: 'Task views', parentSlug: 'tasks' }))
    fireEvent.change(screen.getByLabelText(/Parent capability/), { target: { value: '' } })
    save()
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(ops()).toEqual([{ op: 'reparent', slug: 'task-views', parentSlug: null }])
  })

  it('does not offer the capability itself or anything under it as a parent', () => {
    mount()
    // A cycle the server would 422 anyway ("a capability cannot sit under
    // itself or its descendants") — better never offered than refused.
    expect(screen.queryByRole('option', { name: /Tasks/ })).toBeNull()
    expect(screen.queryByRole('option', { name: /Task views/ })).toBeNull()
    expect(screen.queryByRole('option', { name: /Filters/ })).toBeNull()
    expect(screen.getByRole('option', { name: /Billing/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '— root —' })).toBeInTheDocument()
  })

  it('stops at the first refused op, keeping the dialog and the typed values', async () => {
    const refusal = new ApiRequestError(409, { detail: { error: 'conflict', message: 'head moved' } })
    h.mutateAsync = vi.fn().mockRejectedValue(refusal)
    const onClose = mount()
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Task management' } })
    fireEvent.change(screen.getByLabelText(/Summary/), { target: { value: 'New words.' } })
    save()
    expect(await screen.findByRole('alert')).toHaveTextContent('mapped:')
    expect(h.errorMessage).toHaveBeenCalledWith(refusal, expect.any(String))
    // The summary op never leaves: the rename it depends on did not land.
    expect(ops()).toEqual([{ op: 'rename', slug: 'tasks', title: 'Task management' }])
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByLabelText(/Title/)).toHaveValue('Task management')
  })

  // Each op is its own commit, so the refusal does not roll back what already
  // landed — and the map, being an index, still shows the pre-edit title. A
  // retry that re-sent the rename would write a second commit for a change that
  // is already on the branch.
  it('a retry after a partial failure sends only the op that did not land', async () => {
    const refusal = new ApiRequestError(422, { detail: { error: 'parent_cycle', message: 'no' } })
    h.mutateAsync = vi.fn()
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(refusal)
    const onClose = mount(cap({ slug: 'task-views', title: 'Task views', parentSlug: 'tasks' }))
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Views' } })
    fireEvent.change(screen.getByLabelText(/Parent capability/), { target: { value: 'billing' } })
    save()
    expect(await screen.findByRole('alert')).toHaveTextContent('mapped:')
    expect(ops()).toEqual([
      { op: 'rename', slug: 'task-views', title: 'Views' },
      { op: 'reparent', slug: 'task-views', parentSlug: 'billing' },
    ])

    // The rename is now the dialog's seed even though the prop still says "Task
    // views", so only the reparent is still a change.
    h.mutateAsync.mockResolvedValue({})
    save()
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(ops().slice(2)).toEqual([{ op: 'reparent', slug: 'task-views', parentSlug: 'billing' }])
  })

  it('refuses to clear a summary the server would not accept as blank', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Summary/), { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.getByText(/cannot be emptied/)).toBeInTheDocument()
  })

  it('renders nothing when closed', () => {
    render(<EditCapabilityDialog product={kindash} capability={tasks} open={false} onClose={vi.fn()} />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
