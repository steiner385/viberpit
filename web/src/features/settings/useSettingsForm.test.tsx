import * as React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, Link, RouterProvider } from 'react-router-dom'
import { useSettingsForm, nextDraft } from './useSettingsForm'
import { SaveBar } from './chassis/SaveBar'

type Draft = { repoRoot: string; maxActive: number }

function Harness({ initial, save, beforeSave }: {
  initial: Draft | undefined
  save: (d: Draft) => Promise<unknown>
  beforeSave?: (d: Draft, i: Draft) => boolean | Promise<boolean>
}) {
  const form = useSettingsForm<Draft>({ initial, save, beforeSave })
  if (!form.draft) return <p>loading</p>
  return (
    <div>
      <input aria-label="Repo" value={form.draft.repoRoot} onChange={(e) => form.set('repoRoot', e.target.value)} />
      {form.error && <p role="alert">{form.error}</p>}
      <SaveBar form={form} />
      <Link to="/elsewhere">leave</Link>
    </div>
  )
}

/**
 * A parent that owns `initial` the way a query hook does, so a "refetch" is a
 * new `initial` on the SAME mounted form (not a remount, which would prove
 * nothing about re-seeding).
 */
function Refetchable({ save, start }: { save: (d: Draft) => Promise<unknown>; start?: Draft }) {
  const [initial, setInitial] = React.useState<Draft | undefined>(start)
  return (
    <>
      <Harness initial={initial} save={save} />
      <button type="button" onClick={() => setInitial({ repoRoot: '/server', maxActive: 5 })}>refetch</button>
    </>
  )
}

function mount(ui: React.ReactElement) {
  const router = createMemoryRouter([
    { path: '/', element: ui },
    { path: '/elsewhere', element: <p>elsewhere</p> },
  ])
  render(<RouterProvider router={router} />)
  return router
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useSettingsForm + SaveBar', () => {
  it('starts clean, becomes dirty on edit, saves, and reports Saved', async () => {
    const save = vi.fn().mockResolvedValue({})
    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={save} />)
    const saveBtn = screen.getByRole('button', { name: 'Save' })
    expect(saveBtn).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    expect(saveBtn).toBeEnabled()
    expect(screen.getByRole('status')).toHaveTextContent('Unsaved changes')
    await userEvent.click(saveBtn)
    expect(save).toHaveBeenCalledWith({ repoRoot: '/ab', maxActive: 5 })
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Saved/))
    // A successful save makes the form clean again, even before a refetch lands.
    expect(saveBtn).toBeDisabled()
  })

  it('Discard restores the initial value', async () => {
    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={vi.fn()} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'zzz')
    await userEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.getByLabelText('Repo')).toHaveValue('/a')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('a failed save keeps the draft and shows the error', async () => {
    const save = vi.fn().mockRejectedValue(new Error('boom'))
    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={save} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
    expect(screen.getByLabelText('Repo')).toHaveValue('/ab')
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('blocks in-app navigation while dirty and offers Keep editing / Discard and leave', async () => {
    const router = mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={vi.fn()} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    await userEvent.click(screen.getByRole('link', { name: 'leave' }))
    expect(router.state.location.pathname).toBe('/')
    expect(screen.getByRole('dialog', { name: 'Unsaved changes' })).toBeInTheDocument()

    // Keep editing dismisses the prompt and stays put, with the edit intact.
    await userEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/')
    expect(screen.getByLabelText('Repo')).toHaveValue('/ab')

    await userEvent.click(screen.getByRole('link', { name: 'leave' }))
    await userEvent.click(screen.getByRole('button', { name: 'Discard and leave' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/elsewhere'))
  })

  it('does not block navigation once clean', async () => {
    const router = mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={vi.fn()} />)
    await userEvent.click(screen.getByRole('link', { name: 'leave' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/elsewhere'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('beforeSave returning false aborts the save', async () => {
    const save = vi.fn()
    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={save} beforeSave={() => false} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(save).not.toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent('Unsaved changes')
  })

  /**
   * R8: an async `beforeSave` (a ConfirmDialog) leaves the form dirty and NOT
   * saving while the gate is open, so nothing else stops a second Save click or
   * a Discard. The in-flight latch and the post-gate dirty re-check do.
   */
  it('an async beforeSave latches: a second Save while the gate is open is a no-op (R8)', async () => {
    const save = vi.fn().mockResolvedValue({})
    let release: ((ok: boolean) => void) | undefined
    const beforeSave = vi.fn(() => new Promise<boolean>((resolve) => { release = resolve }))
    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={save} beforeSave={beforeSave} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')

    const saveBtn = screen.getByRole('button', { name: 'Save' })
    await userEvent.click(saveBtn)
    await userEvent.click(saveBtn)
    // The gate is still open, so the form is still dirty and Save is still
    // enabled — the latch, not `saving`, is what makes the second click inert.
    expect(saveBtn).toBeEnabled()
    expect(beforeSave).toHaveBeenCalledTimes(1)

    await act(async () => { release?.(true) })
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith({ repoRoot: '/ab', maxActive: 5 })
  })

  it('Discard while the gate is open cancels the save the gate then approves (R8)', async () => {
    const save = vi.fn().mockResolvedValue({})
    let release: ((ok: boolean) => void) | undefined
    mount(
      <Harness
        initial={{ repoRoot: '/a', maxActive: 5 }}
        save={save}
        beforeSave={() => new Promise<boolean>((resolve) => { release = resolve })}
      />,
    )
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    // Discard is reachable while the gate is open, and it really resets the draft.
    await userEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.getByLabelText('Repo')).toHaveValue('/a')

    // The gate now says yes — to a draft that no longer exists.
    await act(async () => { release?.(true) })
    expect(save).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Repo')).toHaveValue('/a')
  })

  it('seeds the draft when initial arrives', async () => {
    mount(<Refetchable save={vi.fn()} />)
    expect(screen.getByText('loading')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'refetch' }))
    expect(screen.getByLabelText('Repo')).toHaveValue('/server')
  })

  it('a refetch re-seeds a clean draft', async () => {
    mount(<Refetchable save={vi.fn()} start={{ repoRoot: '/a', maxActive: 5 }} />)
    await userEvent.click(screen.getByRole('button', { name: 'refetch' }))
    expect(screen.getByLabelText('Repo')).toHaveValue('/server')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('a refetch does not clobber a dirty draft', async () => {
    mount(<Refetchable save={vi.fn()} start={{ repoRoot: '/a', maxActive: 5 }} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    await userEvent.click(screen.getByRole('button', { name: 'refetch' }))
    expect(screen.getByLabelText('Repo')).toHaveValue('/ab')
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('guards a browser reload only while dirty', async () => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    const beforeUnload = (calls: unknown[][]) => calls.filter((c) => c[0] === 'beforeunload')

    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={vi.fn()} />)
    expect(beforeUnload(add.mock.calls)).toHaveLength(0)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    expect(beforeUnload(add.mock.calls)).toHaveLength(1)
    await userEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(beforeUnload(remove.mock.calls)).toHaveLength(1)
  })
})

describe('nextDraft', () => {
  it('keeps a dirty draft, replaces a clean one', () => {
    expect(nextDraft({ a: 1 }, { a: 1 }, { a: 2 })).toEqual({ a: 2 })      // clean → re-seed
    expect(nextDraft({ a: 1 }, { a: 9 }, { a: 2 })).toEqual({ a: 9 })      // dirty → keep
  })
})
