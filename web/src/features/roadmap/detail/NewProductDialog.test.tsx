// The "+ Product" dialog: what it sends, what it refuses to send, where it goes,
// and what it says when the server refuses. This is the ONLY way to register a
// product now that the Products page is gone, so each of those is load-bearing.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import type { CreateProjectInput } from '@/api/pm'
import { NewProductDialog } from './NewProductDialog'

const create = vi.hoisted(() => ({
  mutate: vi.fn(),
  isPending: false,
  isError: false,
  error: null as unknown,
}))

vi.mock('@/api/hooks', () => ({ useCreateProject: () => create }))

function Here() {
  return <div data-testid="here">{useLocation().pathname}</div>
}

function mount(onClose = vi.fn()) {
  render(
    <MemoryRouter initialEntries={['/roadmap']}>
      <NewProductDialog open onClose={onClose} />
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
})

describe('NewProductDialog', () => {
  it('registers the product with its primary repository and opens it', () => {
    const onClose = mount()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'KinDash' } })
    fireEvent.change(screen.getByLabelText(/Primary repository/), { target: { value: 'cairnea/KinDash' } })
    submit()

    const [input, opts] = create.mutate.mock.calls[0] as [
      CreateProjectInput,
      { onSuccess: (p: { slug: string | null }) => void },
    ]
    expect(input).toEqual({ name: 'KinDash', repos: [{ repo: 'cairnea/KinDash', isPrimary: true }] })

    act(() => opts.onSuccess({ slug: 'kindash' }))
    expect(onClose).toHaveBeenCalled()
    expect(screen.getByTestId('here')).toHaveTextContent('/roadmap/kindash')
  })

  it('sends a description when there is one, and omits it when there is not', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'ViberPit' } })
    fireEvent.change(screen.getByLabelText(/Primary repository/), { target: { value: 'steiner385/viberpit' } })
    fireEvent.change(screen.getByLabelText(/Description/), { target: { value: '  Agent sessions  ' } })
    submit()
    expect(create.mutate.mock.calls[0]![0]).toEqual({
      name: 'ViberPit',
      description: 'Agent sessions',
      repos: [{ repo: 'steiner385/viberpit', isPrimary: true }],
    })
  })

  it('refuses a repository that is not owner/repo, and says why', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'KinDash' } })
    fireEvent.change(screen.getByLabelText(/Primary repository/), { target: { value: 'KinDash' } })
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    expect(screen.getByText(/Use owner\/repo/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Primary repository/)).toHaveAttribute('aria-invalid', 'true')
    submit()
    expect(create.mutate).not.toHaveBeenCalled()
  })

  it('refuses to submit without a name, and does not scold an untouched repo field', () => {
    mount()
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    expect(screen.queryByText(/Use owner\/repo/)).toBeNull()
    submit()
    expect(create.mutate).not.toHaveBeenCalled()
  })

  it('stays open on a payload with no slug — there is nowhere to navigate to', () => {
    const onClose = mount()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'KinDash' } })
    fireEvent.change(screen.getByLabelText(/Primary repository/), { target: { value: 'cairnea/KinDash' } })
    submit()
    const [, opts] = create.mutate.mock.calls[0] as [
      CreateProjectInput,
      { onSuccess: (p: { slug: string | null }) => void },
    ]
    act(() => opts.onSuccess({ slug: null }))
    expect(onClose).toHaveBeenCalled()
    expect(screen.getByTestId('here')).toHaveTextContent('/roadmap')
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
        <NewProductDialog open={false} onClose={vi.fn()} />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
