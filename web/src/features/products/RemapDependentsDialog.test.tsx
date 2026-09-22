import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RemapDependentsDialog } from './RemapDependentsDialog'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks')

const PRODUCT = {
  slug: 'kindash',
  initiatives: [
    { slug: 'init-a', title: 'Init A', primaryCapability: 'dictation', capabilities: ['dictation'] },
    { slug: 'init-b', title: 'Init B', primaryCapability: 'sessions', capabilities: ['sessions'] },
  ],
} as never

const CAPABILITY = { id: 'f2', slug: 'dictation', title: 'Dictation' } as never
const ALL_CAPABILITIES = [
  { id: 'f2', slug: 'dictation', title: 'Dictation' },
  { id: 'f1', slug: 'sessions', title: 'Sessions' },
] as never

describe('RemapDependentsDialog', () => {
  it('re-maps the selected initiatives and issues onto the target capability', async () => {
    const updateInitiative = vi.fn().mockResolvedValue({})
    const updateOverlay = vi.fn().mockResolvedValue({})
    vi.mocked(hooks.useUpdateInitiative).mockReturnValue({
      mutateAsync: updateInitiative, isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdateInitiative>)
    vi.mocked(hooks.useUpdateOverlay).mockReturnValue({
      mutateAsync: updateOverlay, isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdateOverlay>)
    vi.mocked(hooks.useLeaves).mockReturnValue({
      data: [{ id: 'wi-1', kind: 'issue', title: 'Issue 1', featureId: 'dictation' }],
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useLeaves>)

    render(
      <RemapDependentsDialog
        product={PRODUCT}
        capability={CAPABILITY}
        allCapabilities={ALL_CAPABILITIES}
        open
        onClose={() => {}}
      />,
    )

    // Only the initiative touching `dictation` (init-a) is offered.
    expect(screen.getByRole('checkbox', { name: /init a/i })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /init b/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: /init a/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /issue 1/i }))
    fireEvent.change(screen.getByRole('combobox', { name: /move to/i }), {
      target: { value: 'sessions' },
    })
    fireEvent.click(screen.getByRole('button', { name: /move selected/i }))

    await waitFor(() => {
      expect(updateInitiative).toHaveBeenCalledWith({
        slug: 'init-a', input: { capabilityIds: ['sessions'], primaryCapabilityId: 'sessions' },
      })
      expect(updateOverlay).toHaveBeenCalledWith({ itemId: 'wi-1', edit: { featureId: 'sessions' } })
    })
  })

  it('disables Move selected until a target capability and at least one item are chosen', () => {
    vi.mocked(hooks.useUpdateInitiative).mockReturnValue({
      mutateAsync: vi.fn(), isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdateInitiative>)
    vi.mocked(hooks.useUpdateOverlay).mockReturnValue({
      mutateAsync: vi.fn(), isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdateOverlay>)
    vi.mocked(hooks.useLeaves).mockReturnValue({
      data: [{ id: 'wi-1', kind: 'issue', title: 'Issue 1', featureId: 'dictation' }],
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useLeaves>)

    render(
      <RemapDependentsDialog
        product={PRODUCT}
        capability={CAPABILITY}
        allCapabilities={ALL_CAPABILITIES}
        open
        onClose={() => {}}
      />,
    )

    expect(screen.getByRole('button', { name: /move selected/i })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /issue 1/i }))
    expect(screen.getByRole('button', { name: /move selected/i })).toBeDisabled()
    fireEvent.change(screen.getByRole('combobox', { name: /move to/i }), {
      target: { value: 'sessions' },
    })
    expect(screen.getByRole('button', { name: /move selected/i })).toBeEnabled()
  })
})
