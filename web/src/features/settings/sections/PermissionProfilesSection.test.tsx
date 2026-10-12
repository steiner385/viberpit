import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as hooks from '@/api/hooks'
import { ApiRequestError } from '@/api/client'
import { PermissionProfilesSection } from './PermissionProfilesSection'

vi.mock('@/api/hooks', () => ({
  usePermissionProfiles: vi.fn(),
  useCreatePermissionProfile: vi.fn(),
  useUpdatePermissionProfile: vi.fn(),
  useDeletePermissionProfile: vi.fn(),
}))

const mutation = (mutateAsync: (...a: unknown[]) => Promise<unknown>) =>
  ({ mutateAsync, mutate: vi.fn(), isPending: false }) as never

describe('PermissionProfilesSection', () => {
  const create = vi.fn()

  beforeEach(() => {
    create.mockReset()
    vi.mocked(hooks.usePermissionProfiles).mockReturnValue({ data: [], isLoading: false } as never)
    vi.mocked(hooks.useCreatePermissionProfile).mockReturnValue(mutation(create))
    vi.mocked(hooks.useUpdatePermissionProfile).mockReturnValue(mutation(vi.fn()))
    vi.mocked(hooks.useDeletePermissionProfile).mockReturnValue(mutation(vi.fn()))
  })

  it('shows why a refused pattern could not be saved and keeps the form open', async () => {
    create.mockRejectedValue(new ApiRequestError(400, {
      detail: {
        error: 'pattern_refused',
        message: 'Some patterns are too broad or unsafe to allow.',
        reasons: ['Bash(git *): pushes must be granted as an exact command'],
      },
    }))
    const user = userEvent.setup()
    render(<PermissionProfilesSection />)
    await user.click(screen.getByRole('button', { name: '+ New profile' }))
    await user.type(screen.getByPlaceholderText('e.g. read-only research'), 'git')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Some patterns are too broad or unsafe to allow.')
    expect(alert).toHaveTextContent('Bash(git *): pushes must be granted as an exact command')
    expect(screen.getByRole('button', { name: 'Create' })).toBeInTheDocument()
  })

  it('closes the form on a successful save', async () => {
    create.mockResolvedValue({})
    const user = userEvent.setup()
    render(<PermissionProfilesSection />)
    await user.click(screen.getByRole('button', { name: '+ New profile' }))
    await user.type(screen.getByPlaceholderText('e.g. read-only research'), 'tests')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    expect(create).toHaveBeenCalledWith(expect.objectContaining({ name: 'tests', patterns: [] }))
    expect(screen.queryByRole('button', { name: 'Create' })).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
