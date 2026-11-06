/**
 * The Environment card's worktree Discard / Repair (D-19): shown only where the
 * preflight says the server would act; Discard confirms with the facts (counts,
 * branch, restore, ignored files) and an explicit tick; every refusal reads plainly.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }) }))
import { toast } from 'sonner'
import { api, ApiRequestError } from '@/api/client'
import type { WorktreePreflight } from '@/api/types'
import { WorktreeControls } from './WorktreeControls'
import { IGNORED_FILES_LOST, SESSION_ENDED, WORKTREE_ERROR_MESSAGES } from './worktreeCopy'

const PF: WorktreePreflight = {
  sessionId: 's1', path: '/home/u/worktrees/s1', managed: true, present: true, state: 'present',
  branch: 'viberpit/s1', sessionBranch: true, dirty: 3, unpushed: 2, recoveryRefs: [],
  restoreHint: 'git -C /repo worktree add <dir> <recovery-ref>', busy: false, canDiscard: true, canRepair: false,
}
const DISCARDED = {
  discarded: true, recoveryRefs: ['refs/viberpit/recovery/s1-discard', 'refs/viberpit/recovery/s1-tip'],
  restoreCommand: 'git -C /repo worktree add ../s1 refs/viberpit/recovery/s1-discard',
  path: PF.path, branch: 'viberpit/s1', branchDeleted: true,
}

let preflight: unknown
let posts: { path: string; body: unknown }[]
let reply: unknown

beforeEach(() => {
  vi.mocked(toast.success).mockReset()
  vi.mocked(toast.error).mockReset()
  preflight = PF
  posts = []
  reply = DISCARDED
  vi.spyOn(api, 'get').mockImplementation(async () => {
    if (preflight instanceof Error) throw preflight
    return preflight
  })
  vi.spyOn(api, 'post').mockImplementation(async (path: string, body?: unknown) => {
    posts.push({ path, body })
    if (reply instanceof Error) throw reply
    return reply
  })
})

function mount(busy = false) {
  const onDiscarded = vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <WorktreeControls sessionId="s1" busy={busy} onDiscarded={onDiscarded} />
    </QueryClientProvider>,
  )
  return { onDiscarded }
}

async function openDiscard() {
  await userEvent.click(await screen.findByRole('button', { name: 'Discard…' }))
  const dialog = await screen.findByRole('dialog', { name: 'Discard this worktree?' })
  await within(dialog).findByTestId('discard-facts')
  return dialog
}

describe('WorktreeControls — visibility', () => {
  it('a present managed worktree offers Discard and no Repair', async () => {
    mount()
    expect(await screen.findByRole('button', { name: 'Discard…' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Repair' })).toBeNull()
  })

  it('a token caller (403 preflight) sees no controls at all', async () => {
    preflight = new ApiRequestError(403, { detail: { code: 'SESSION_CONTROL_HUMAN_ONLY', message: 'x' } })
    mount()
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(screen.queryByTestId('worktree-controls')).toBeNull()
  })

  it('a cwd ViberPit did not create offers nothing', async () => {
    preflight = { ...PF, managed: false, state: 'not_a_worktree', canDiscard: false }
    mount()
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(screen.queryByTestId('worktree-controls')).toBeNull()
  })
})

describe('WorktreeControls — Repair', () => {
  it('shows only for a missing worktree the server can recreate, and posts it', async () => {
    preflight = { ...PF, present: false, state: 'missing', canDiscard: false, canRepair: true }
    reply = { repaired: true, path: PF.path, branch: 'viberpit/s1', recoveryRefs: [] }
    mount()
    await userEvent.click(await screen.findByRole('button', { name: 'Repair' }))
    expect(screen.queryByRole('button', { name: 'Discard…' })).toBeNull()
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Worktree recreated on viberpit/s1'))
    expect(posts).toEqual([{ path: '/sessions/s1/worktree/repair', body: undefined }])
  })

  it('a missing worktree the server cannot recreate offers no Repair', async () => {
    preflight = { ...PF, present: false, state: 'missing', canDiscard: false, canRepair: false }
    mount()
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: 'Repair' })).toBeNull()
  })

  it('a refused repair says why', async () => {
    preflight = { ...PF, present: false, state: 'missing', canDiscard: false, canRepair: true }
    reply = new ApiRequestError(409, { detail: { code: 'WORKTREE_REPAIR_FAILED', message: 'server' } })
    mount()
    await userEvent.click(await screen.findByRole('button', { name: 'Repair' }))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(WORKTREE_ERROR_MESSAGES.WORKTREE_REPAIR_FAILED))
  })
})

describe('WorktreeControls — Discard', () => {
  it('confirms with the counts, the branch, the restore hint and the ignored-files warning', async () => {
    mount()
    const dialog = await openDiscard()
    expect(within(dialog).getByText('3 uncommitted files')).toBeInTheDocument()
    expect(within(dialog).getByText('2 unpushed commits')).toBeInTheDocument()
    expect(within(dialog).getByText('Branch viberpit/s1 will be deleted.')).toBeInTheDocument()
    expect(within(dialog).getByText(PF.restoreHint!)).toBeInTheDocument()
    expect(within(dialog).getByText(IGNORED_FILES_LOST)).toBeInTheDocument()
    expect(IGNORED_FILES_LOST).toMatch(/\.env/)
  })

  it("a user's own branch is kept, and unreadable counts say so", async () => {
    preflight = { ...PF, branch: 'feature', sessionBranch: false, dirty: null, unpushed: 0 }
    mount()
    const dialog = await openDiscard()
    expect(within(dialog).getByText('Branch feature will be kept.')).toBeInTheDocument()
    expect(within(dialog).getByText("uncommitted files: couldn't be read")).toBeInTheDocument()
    expect(within(dialog).getByText('0 unpushed commits')).toBeInTheDocument()
  })

  it('needs the explicit tick, posts the confirm, leaves the session and toasts the recovery refs', async () => {
    const { onDiscarded } = mount()
    const dialog = await openDiscard()
    const discard = within(dialog).getByRole('button', { name: 'Discard' })
    expect(discard).toBeDisabled()
    await userEvent.click(within(dialog).getByLabelText('End the session and delete its worktree'))
    await userEvent.click(discard)
    await waitFor(() => expect(onDiscarded).toHaveBeenCalledTimes(1))
    expect(posts).toEqual([{ path: '/sessions/s1/worktree/discard', body: { confirm: true } }])
    expect(toast.success).toHaveBeenCalledWith('Discarded — worktree and branch viberpit/s1 removed',
      expect.objectContaining({ duration: Infinity }))
    // The description carries every ref and the restore command (copy on click).
    const { description } = vi.mocked(toast.success).mock.calls[0][1] as { description: React.ReactElement }
    render(<>{description}</>)
    expect(screen.getByText(/refs\/viberpit\/recovery\/s1-discard, refs\/viberpit\/recovery\/s1-tip/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: DISCARDED.restoreCommand })).toHaveAttribute('title', 'Copy restore command')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('a busy session cannot be discarded yet, and says why', async () => {
    preflight = { ...PF, busy: true, canDiscard: false }
    mount(true)
    const dialog = await openDiscard()
    expect(within(dialog).getByTestId('discard-refusal')).toHaveTextContent(WORKTREE_ERROR_MESSAGES.WORKTREE_SESSION_BUSY)
    expect(within(dialog).getByLabelText('End the session and delete its worktree')).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Discard' })).toBeDisabled()
  })

  it.each([
    ['WORKTREE_DISCARD_UNSAFE', new ApiRequestError(409, { detail: { code: 'WORKTREE_DISCARD_UNSAFE', message: 's' } }),
      WORKTREE_ERROR_MESSAGES.WORKTREE_DISCARD_UNSAFE],
    ['WORKTREE_SESSION_BUSY', new ApiRequestError(409, { detail: { code: 'WORKTREE_SESSION_BUSY', message: 's' } }),
      WORKTREE_ERROR_MESSAGES.WORKTREE_SESSION_BUSY],
    ['WORKTREE_NOT_MANAGED', new ApiRequestError(409, { detail: { code: 'WORKTREE_NOT_MANAGED', message: 's' } }),
      WORKTREE_ERROR_MESSAGES.WORKTREE_NOT_MANAGED],
    ['SESSION_MOVING', new ApiRequestError(409, { detail: { code: 'SESSION_MOVING', message: 's', movingTo: 'rocky' } }),
      'Moving to rocky…'],
    ['an ended session (404)', new ApiRequestError(404, { detail: 'session not found' }), SESSION_ENDED],
    ['an unknown code', new ApiRequestError(500, { detail: { code: 'SOMETHING_NEW', message: 'The server says so.' } }),
      'The server says so.'],
    ['no message at all', new ApiRequestError(502, {}), "Couldn't discard the worktree"],
  ])('a refused discard (%s) says so in place and stays on the session', async (_name, err, text) => {
    reply = err
    const { onDiscarded } = mount()
    const dialog = await openDiscard()
    await userEvent.click(within(dialog).getByLabelText('End the session and delete its worktree'))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Discard' }))
    expect(await within(dialog).findByTestId('discard-refusal')).toHaveTextContent(text)
    expect(onDiscarded).not.toHaveBeenCalled()
  })

  it('a failure after the refs were written lists them and the restore command', async () => {
    reply = new ApiRequestError(500, { detail: {
      code: 'WORKTREE_REMOVE_FAILED', message: 's',
      recoveryRefs: ['refs/viberpit/recovery/s1-discard'], restoreCommand: 'git worktree add x refs/viberpit/recovery/s1-discard',
    } })
    mount()
    const dialog = await openDiscard()
    await userEvent.click(within(dialog).getByLabelText('End the session and delete its worktree'))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Discard' }))
    const refs = await within(dialog).findByTestId('discard-recovery')
    expect(refs).toHaveTextContent('refs/viberpit/recovery/s1-discard')
    expect(within(refs).getByRole('button', { name: 'git worktree add x refs/viberpit/recovery/s1-discard' })).toBeInTheDocument()
    expect(within(dialog).getByTestId('discard-refusal')).toHaveTextContent(WORKTREE_ERROR_MESSAGES.WORKTREE_REMOVE_FAILED)
  })
})
