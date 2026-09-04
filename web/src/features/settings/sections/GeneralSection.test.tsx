/**
 * Settings › General on the chassis (spec §8, task 13).
 *
 * The hooks are mocked here rather than the transport: this suite is about the
 * SECTION — its placeholders, its payload and its archive-move gate — and a
 * fixed `useUpdateSettings` spy is what lets the payload be asserted key by key.
 * `SettingsPage.test.tsx` covers the section inside the real page with the real
 * hooks running against a faked `api`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as hooks from '@/api/hooks'
import { appSettings } from '@/test/factories'
import { queryResult, mutationResult } from '@/test/reactQuery'
import { GeneralSection } from './GeneralSection'

vi.mock('@/api/hooks', () => ({
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useArchivedSessions: vi.fn(),
}))

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/', element: <GeneralSection /> }])
  return render(<QueryClientProvider client={qc}><RouterProvider router={router} /></QueryClientProvider>)
}

describe('GeneralSection', () => {
  const mutateAsync = vi.fn().mockResolvedValue({})
  beforeEach(() => {
    mutateAsync.mockClear()
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      repoRoot: null, archiveHome: null, maxArchivedSessions: 100, maxActiveSessions: null, autoArchiveEnabled: true,
      effectiveDefaults: { repoRoot: '/home/tony/GitHub', archiveHome: '/var/ccswitch/archives', modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable', maxActiveSessions: 5 },
    })))
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(mutationResult({ mutateAsync }))
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult(Array.from({ length: 3 }, (_, i) => ({ id: `s${i}` })) as never))
  })

  it('shows effective defaults as placeholders and a Reset that clears to default', async () => {
    mount()
    expect(screen.getByRole('heading', { level: 2, name: 'General' })).toBeInTheDocument()
    const repo = screen.getByLabelText('Repo clone root')
    expect(repo).toHaveAttribute('placeholder', '/home/tony/GitHub · server default')
    expect(screen.getByLabelText('Archive storage path')).toHaveAttribute('placeholder', '/var/ccswitch/archives · server default')
    await userEvent.type(repo, '/srv/x')
    await userEvent.click(screen.getByRole('button', { name: 'Reset repo clone root to default' }))
    expect(repo).toHaveValue('')
  })

  it('with no configured archive home the placeholder is just "Server default" (R13)', () => {
    // The common deployment: the server has no ARCHIVE_HOME, so there is no path
    // to name and the suffix form would read "Server default · server default".
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      effectiveDefaults: { repoRoot: '/home/tony/GitHub', archiveHome: null, modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable', maxActiveSessions: 5 },
    })))
    mount()
    expect(screen.getByLabelText('Archive storage path')).toHaveAttribute('placeholder', 'Server default')
  })

  it('saves the same payload shape as before (blank → 0 / null)', async () => {
    mount()
    await userEvent.type(screen.getByLabelText('Max active sessions'), '7')
    await userEvent.clear(screen.getByLabelText('Max archived sessions'))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Auto-archive completed sessions' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({
      repoRoot: null, archiveHome: '', maxArchivedSessions: 0, maxActiveSessions: 7, autoArchiveEnabled: false,
    }))
  })

  it('changing the archive path asks for a typed confirmation naming the archive count (S16)', async () => {
    mount()
    await userEvent.type(screen.getByLabelText('Archive storage path'), '/mnt/slow')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const dlg = await screen.findByRole('dialog', { name: 'Move archived sessions?' })
    expect(dlg).toHaveTextContent('about 3 archived sessions')
    expect(dlg).toHaveTextContent('/mnt/slow')
    expect(mutateAsync).not.toHaveBeenCalled()
    await userEvent.type(screen.getByLabelText('Type /mnt/slow to confirm'), '/mnt/slow')
    await userEvent.click(screen.getByRole('button', { name: 'Move archives' }))
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ archiveHome: '/mnt/slow' })))
  })

  it('a 409 from the move keeps the draft and shows the server detail', async () => {
    const { ApiRequestError } = await import('@/api/client')
    mutateAsync.mockRejectedValueOnce(new ApiRequestError(409, { detail: { code: 'archive_move_failed', moved: 2, failed: 1, total: 3 } }))
    mount()
    await userEvent.type(screen.getByLabelText('Max active sessions'), '9')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Request failed (409)')
    expect(screen.getByLabelText('Max active sessions')).toHaveValue(9)
  })
})
