import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RemapDependentsDialog } from './RemapDependentsDialog'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks')

const PRODUCT = {
  slug: 'kindash',
  initiatives: [
    // Touches ONLY `dictation`, and it's primary there.
    { slug: 'init-a', title: 'Init A', primaryCapability: 'dictation', capabilities: ['dictation'] },
    // Doesn't touch `dictation` at all — must not be offered.
    { slug: 'init-b', title: 'Init B', primaryCapability: 'sessions', capabilities: ['sessions'] },
    // Touches `dictation` (primary) AND an unrelated `notifications` — the
    // `notifications` touch must survive the re-map.
    {
      slug: 'init-c', title: 'Init C', primaryCapability: 'dictation',
      capabilities: ['dictation', 'notifications'],
    },
    // Touches `dictation` (secondary — `notifications` is primary) AND
    // `notifications` — `notifications` touch survives, and because
    // `dictation` was never this initiative's primary, `primaryCapabilityId`
    // must NOT be sent at all.
    {
      slug: 'init-d', title: 'Init D', primaryCapability: 'notifications',
      capabilities: ['dictation', 'notifications'],
    },
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

    // Only the initiatives touching `dictation` are offered — init-b (which
    // touches only `sessions`) is not.
    expect(screen.getByRole('checkbox', { name: /^init a/i })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /^init b/i })).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /^init c/i })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /^init d/i })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: /^init a/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /^init c/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /^init d/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /issue 1/i }))
    fireEvent.change(screen.getByRole('combobox', { name: /move to/i }), {
      target: { value: 'sessions' },
    })
    fireEvent.click(screen.getByRole('button', { name: /move selected/i }))

    await waitFor(() => {
      // init-a touched ONLY `dictation`, and `dictation` was its primary:
      // capabilityIds collapses to just the target, and the primary moves too.
      expect(updateInitiative).toHaveBeenCalledWith({
        slug: 'init-a', input: { capabilityIds: ['sessions'], primaryCapabilityId: 'sessions' },
      })
      // init-c also touched `notifications` — that touch must survive — and
      // `dictation` was its primary, so the primary moves to the target too.
      expect(updateInitiative).toHaveBeenCalledWith({
        slug: 'init-c',
        input: { capabilityIds: ['notifications', 'sessions'], primaryCapabilityId: 'sessions' },
      })
      // init-d also touched `notifications` — survives — but `dictation` was
      // NOT its primary (`notifications` was), so no primaryCapabilityId is
      // sent at all: this initiative's primary genuinely isn't changing.
      expect(updateInitiative).toHaveBeenCalledWith({
        slug: 'init-d',
        input: { capabilityIds: ['notifications', 'sessions'] },
      })
      const initDCall = updateInitiative.mock.calls.find(([arg]) => arg.slug === 'init-d')
      expect(initDCall?.[0].input).not.toHaveProperty('primaryCapabilityId')
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
