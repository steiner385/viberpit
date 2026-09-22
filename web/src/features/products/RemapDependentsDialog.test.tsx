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
    // Touches `dictation` plus a slug that is NOT in `allCapabilities` (stale
    // / dangling) — the unresolvable slug must be dropped rather than sent as
    // an `undefined` the API would reject.
    {
      slug: 'init-e', title: 'Init E', primaryCapability: 'dictation',
      capabilities: ['dictation', 'ghost-cap'],
    },
  ],
} as never

// Both endpoints the dialog calls key on `Feature.id`, never on the slug, so
// every assertion below is in terms of ids ('f1'/'f3'), not slugs.
const CAPABILITY = { id: 'f2', slug: 'dictation', title: 'Dictation' } as never
const ALL_CAPABILITIES = [
  { id: 'f2', slug: 'dictation', title: 'Dictation' },
  { id: 'f1', slug: 'sessions', title: 'Sessions' },
  { id: 'f3', slug: 'notifications', title: 'Notifications' },
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

    // The "Move to" options carry Feature ids, never slugs.
    const target = screen.getByRole('combobox', { name: /move to/i })
    expect(
      Array.from(target.querySelectorAll('option')).map((o) => o.value),
    ).toEqual(['', 'f1', 'f3'])

    fireEvent.click(screen.getByRole('checkbox', { name: /^init a/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /^init c/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /^init d/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /^init e/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /issue 1/i }))
    fireEvent.change(target, { target: { value: 'f1' } })
    fireEvent.click(screen.getByRole('button', { name: /move selected/i }))

    await waitFor(() => {
      // init-a touched ONLY `dictation`, and `dictation` was its primary:
      // capabilityIds collapses to just the target id, and the primary moves too.
      expect(updateInitiative).toHaveBeenCalledWith({
        slug: 'init-a', input: { capabilityIds: ['f1'], primaryCapabilityId: 'f1' },
      })
      // init-c also touched `notifications` — that touch must survive, as its
      // id — and `dictation` was its primary, so the primary moves too.
      expect(updateInitiative).toHaveBeenCalledWith({
        slug: 'init-c',
        input: { capabilityIds: ['f3', 'f1'], primaryCapabilityId: 'f1' },
      })
      // init-d also touched `notifications` — survives — but `dictation` was
      // NOT its primary (`notifications` was), so no primaryCapabilityId is
      // sent at all: this initiative's primary genuinely isn't changing.
      expect(updateInitiative).toHaveBeenCalledWith({
        slug: 'init-d',
        input: { capabilityIds: ['f3', 'f1'] },
      })
      const initDCall = updateInitiative.mock.calls.find(([arg]) => arg.slug === 'init-d')
      expect(initDCall?.[0].input).not.toHaveProperty('primaryCapabilityId')
      // init-e's dangling `ghost-cap` slug resolves to nothing and is dropped,
      // leaving just the target — no `undefined` leaks into the payload.
      expect(updateInitiative).toHaveBeenCalledWith({
        slug: 'init-e', input: { capabilityIds: ['f1'], primaryCapabilityId: 'f1' },
      })
      expect(updateOverlay).toHaveBeenCalledWith({ itemId: 'wi-1', edit: { featureId: 'f1' } })
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
      target: { value: 'f1' },
    })
    expect(screen.getByRole('button', { name: /move selected/i })).toBeEnabled()
  })
})
