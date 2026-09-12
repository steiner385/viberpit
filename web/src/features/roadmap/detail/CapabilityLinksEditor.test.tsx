// A capability's links to other capabilities (spec §8 "Capability links — chip
// editor on Overview"): the chips it has, a remove on each, and one row to add
// another. Every change sends the WHOLE list as one `links` op — the server
// replaces, it does not merge.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import type { MapChangeInput } from '@/api/pm'
import { cap, product } from '../__fixtures__/map'
import { CapabilityLinksEditor } from './CapabilityLinksEditor'

const h = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  isPending: false,
  errorMessage: vi.fn((_e: unknown, fallback: string) => `mapped:${fallback}`),
}))
vi.mock('@/api/hooks', () => ({
  useEditMapChanges: () => ({ mutateAsync: h.mutateAsync, isPending: h.isPending, isError: false, error: null }),
  useRoadmapMap: () => ({ data: MAP }),
  mapChangesErrorMessage: (e: unknown, fallback: string) => h.errorMessage(e, fallback),
}))

const tasks = cap({
  slug: 'tasks', title: 'Tasks',
  links: [
    { kind: 'powered_by', projectSlug: 'viberpit', featureSlug: 'sessions' },
    { kind: 'integrates_with', projectSlug: 'kindash', featureSlug: 'billing' },
  ],
})
const kindash = product({ slug: 'kindash', name: 'KinDash', capabilities: [tasks, cap({ slug: 'billing', title: 'Billing' })] })
const viberpit = product({
  slug: 'viberpit', name: 'ViberPit',
  capabilities: [cap({ slug: 'sessions', title: 'Sessions' }), cap({ slug: 'roadmap', title: 'Roadmap' })],
})
const MAP = { generatedAt: 'now', products: [kindash, viberpit] }

const mount = (capability = tasks) =>
  render(<MemoryRouter><CapabilityLinksEditor product={kindash} capability={capability} /></MemoryRouter>)
const sent = (n = 0) => h.mutateAsync.mock.calls[n]?.[0] as MapChangeInput | undefined

beforeEach(() => {
  h.mutateAsync = vi.fn().mockResolvedValue({})
  h.isPending = false
  h.errorMessage.mockClear()
})

describe('CapabilityLinksEditor', () => {
  it('renders each link as a chip that opens the capability it points at', () => {
    mount()
    expect(screen.getByRole('link', { name: /viberpit\/sessions/ }))
      .toHaveAttribute('href', '/roadmap/viberpit/capabilities/sessions')
    expect(screen.getByText(/powered by · viberpit\/sessions/)).toBeInTheDocument()
  })

  it('a remove sends the list minus that one', async () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Remove link: powered by · viberpit/sessions' }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalled())
    // The API names the target `project`/`feature`, not the map's
    // `projectSlug`/`featureSlug` — the conversion is this editor's job.
    expect(sent()).toEqual({
      op: 'links', slug: 'tasks',
      links: [{ kind: 'integrates_with', project: 'kindash', feature: 'billing' }],
    })
  })

  it('an add sends the whole list with the new one on the end', async () => {
    mount()
    // Nothing is chosen yet, so there is nothing to add.
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Product'), { target: { value: 'viberpit' } })
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Capability'), { target: { value: 'roadmap' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalled())
    expect(sent()).toEqual({
      op: 'links', slug: 'tasks',
      links: [
        { kind: 'powered_by', project: 'viberpit', feature: 'sessions' },
        { kind: 'integrates_with', project: 'kindash', feature: 'billing' },
        { kind: 'powered_by', project: 'viberpit', feature: 'roadmap' },
      ],
    })
  })

  it('offers neither the capability itself nor a link it already has', () => {
    mount()
    fireEvent.change(screen.getByLabelText('Product'), { target: { value: 'viberpit' } })
    // `powered_by viberpit/sessions` is already a chip — offering it again
    // would put the same link in the doc twice.
    expect(screen.queryByRole('option', { name: /Sessions/ })).toBeNull()
    expect(screen.getByRole('option', { name: /Roadmap/ })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Product'), { target: { value: 'kindash' } })
    // Its own product's tree, minus itself: a capability cannot link to itself.
    expect(screen.queryByRole('option', { name: /Tasks/ })).toBeNull()
    expect(screen.getByRole('option', { name: /Billing/ })).toBeInTheDocument()
  })

  // The map is INDEXED: the commit lands at once, `capability.links` only
  // catches up on the next pm-sync pass. Basing the second send on the prop
  // would re-send the pre-first-edit list and quietly undo the first edit.
  it('bases a second edit on what it last SENT, not on the still-stale prop', async () => {
    const { rerender } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Remove link: powered by · viberpit/sessions' }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(sent(0)?.links).toEqual([{ kind: 'integrates_with', project: 'kindash', feature: 'billing' }])

    // The chips now show what the DOC holds, not what the map still says.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Remove link: powered by · viberpit/sessions' })).toBeNull())

    // Second remove, prop untouched: the base is the first result, so this sends
    // the empty list — not `[powered_by viberpit/sessions]`.
    fireEvent.click(screen.getByRole('button', { name: 'Remove link: integrates with · kindash/billing' }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(2))
    expect(sent(1)?.links).toEqual([])

    // Once the prop agrees with what was sent, the local copy is dropped and the
    // prop is the base again — otherwise an edit made elsewhere could never show.
    const caughtUp = cap({ slug: 'tasks', title: 'Tasks', links: [] })
    rerender(<MemoryRouter><CapabilityLinksEditor product={kindash} capability={caughtUp} /></MemoryRouter>)
    const withOne = cap({
      slug: 'tasks', title: 'Tasks',
      links: [{ kind: 'powered_by', projectSlug: 'viberpit', featureSlug: 'roadmap' }],
    })
    rerender(<MemoryRouter><CapabilityLinksEditor product={kindash} capability={withOne} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Remove link: powered by · viberpit/roadmap' }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(3))
    expect(sent(2)?.links).toEqual([])
  })

  it('drops the sent copy when the capability itself changes', async () => {
    const { rerender } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Remove link: powered by · viberpit/sessions' }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))

    // A different capability's links are not this one's sent list.
    const other = cap({
      slug: 'billing', title: 'Billing',
      links: [{ kind: 'powered_by', projectSlug: 'viberpit', featureSlug: 'sessions' }],
    })
    rerender(<MemoryRouter><CapabilityLinksEditor product={kindash} capability={other} /></MemoryRouter>)
    expect(screen.getByRole('button', { name: 'Remove link: powered by · viberpit/sessions' })).toBeInTheDocument()
  })

  it('says so when the server refuses the change', async () => {
    const refusal = new ApiRequestError(502, { detail: { error: 'github', step: 'commit', message: 'boom' } })
    h.mutateAsync = vi.fn().mockRejectedValue(refusal)
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Remove link: powered by · viberpit/sessions' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('mapped:')
    expect(h.errorMessage).toHaveBeenCalledWith(refusal, expect.any(String))
  })

  it('says so when there are no links yet', () => {
    mount(cap({ slug: 'tasks', title: 'Tasks' }))
    expect(screen.getByText(/No linked capabilities yet/)).toBeInTheDocument()
  })
})
