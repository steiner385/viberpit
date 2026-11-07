/**
 * Settings › Sessions › Defaults on the chassis (spec §8, task 14).
 *
 * The hooks are mocked rather than the transport, for the same reason as
 * `GeneralSection.test.tsx`: this suite is about the SECTION — what it
 * initialises from, which modes it offers, its unsafe-mode gate and its exact
 * payload — and a fixed `useUpdateSettings` spy is what lets the payload be
 * asserted key by key. `SettingsPage.test.tsx` covers the section inside the
 * real page with the real hooks against a faked `api`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as hooks from '@/api/hooks'
import type { AppSettings } from '@/api/hooks'
import { appSettings } from '@/test/factories'
import { queryResult, idleQueryResult, loadingQueryResult, mutationResult } from '@/test/reactQuery'
import { ApiRequestError } from '@/api/client'
import { BRANCH_PREFIX_COPY, SessionDefaultsSection } from './SessionDefaultsSection'
import { SANDBOX_DEFAULT_COPY } from '@/features/sessions/sandboxCopy'

expect.extend(toHaveNoViolations)

vi.mock('@/api/hooks', () => ({ useSettings: vi.fn(), useUpdateSettings: vi.fn() }))

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/', element: <SessionDefaultsSection /> }])
  return render(<QueryClientProvider client={qc}><RouterProvider router={router} /></QueryClientProvider>)
}

describe('SessionDefaultsSection', () => {
  const mutateAsync = vi.fn().mockResolvedValue({})
  beforeEach(() => {
    mutateAsync.mockClear()
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      defaultPermissionMode: 'interactive', thinkingCollapsed: true, defaultToolDetail: 'auto',
      modelApprovalAction: null, modelApprovalDenylist: null,
      acceptedPermissionModes: ['interactive', 'auto', 'allowlist', 'acceptEdits', 'full-auto'],   // bypass NOT accepted
    })))
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(mutationResult({ mutateAsync }))
  })

  it('initialises from the server (thinkingCollapsed true, not a hard-coded false) (S21)', async () => {
    const { container } = mount()
    expect(screen.getByRole('checkbox', { name: 'Collapse thinking by default' })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Server default/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Server default/ })).toHaveAccessibleName(/require approval/i)
    expect(await axe(container)).toHaveNoViolations()
  })

  it('while /settings is in flight it shows the chassis and a skeleton, not nothing', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(loadingQueryResult<AppSettings>())
    const { container } = mount()
    expect(screen.getByRole('heading', { level: 2, name: 'Defaults' })).toBeInTheDocument()
    expect(container.querySelector('.animate-pulse')).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('a failed FIRST load shows an alert naming the failure, not an empty panel', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(idleQueryResult<AppSettings>({
      isError: true, error: new Error('boom'), status: 'error', isPending: false,
    }))
    mount()
    expect(screen.getByRole('heading', { level: 2, name: 'Defaults' })).toBeInTheDocument()
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Settings could not be loaded')
    expect(alert).toHaveTextContent('boom')
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('a failed REFETCH keeps the form and warns beside it (R17)', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(
      queryResult(appSettings(), { isError: true, error: new Error('boom') }),
    )
    mount()
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(screen.getByLabelText('Default tool approvals')).toBeInTheDocument()
    // Scoped through the banner's own title: SaveBar owns a `role="status"` region
    // too, so a bare role query matches both live regions.
    const notice = screen.getByText('Could not refresh settings').closest('[role="status"]')
    expect(notice).not.toBeNull()
    expect(notice).toHaveTextContent('boom')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('with no gated tiers configured the denylist placeholder is just "Server default" (R14a)', () => {
    // Same reason as R13 in GeneralSection: there is no tier to name, and the
    // suffix form would read "Server default · server default".
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      effectiveDefaults: {
        repoRoot: '/home/tony/GitHub', archiveHome: null, modelApprovalAction: 'require_approval',
        modelApprovalDenylist: '', branchPrefix: 'viberpit/', globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] },
      },
    })))
    mount()
    expect(screen.getByLabelText('Gated models')).toHaveAttribute('placeholder', 'Server default')
  })

  it('disables, with a reason, any mode the server does not accept (DEC-4)', () => {
    mount()
    const opt = screen.getByRole('option', { name: /Bypass/ })
    expect(opt).toBeDisabled()
    expect(screen.getByText(/not accepted by this server/i)).toBeInTheDocument()
  })

  it('choosing full-auto asks for confirmation before saving (S17)', async () => {
    mount()
    await userEvent.selectOptions(screen.getByLabelText('Default tool approvals'), 'full-auto')
    expect(screen.getByRole('alert')).toHaveTextContent(/run code as you/)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('dialog', { name: 'Allow sessions to run without prompting?' })
    expect(mutateAsync).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Use full auto' }))
    expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ defaultPermissionMode: 'full-auto' }))
  })

  // The next two came from `components/settings/__tests__/ModelApprovalControls.test.tsx`
  // when task 18 deleted the legacy General form: the global half of that file
  // asserted the non-null prefill and the null payload, and neither had a
  // counterpart here. They are re-expressed against the radio group (D11/R14b)
  // rather than the old `<select>`, but they guard the same two behaviours.
  it('pre-fills the action from settings when non-null', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({ modelApprovalAction: 'allow' })))
    mount()
    expect(screen.getByRole('radio', { name: 'Allow' })).toBeChecked()
    expect(screen.getByRole('radio', { name: /^Server default/ })).not.toBeChecked()
  })

  it('sends null for modelApprovalAction when left on server default', async () => {
    mount()
    // Dirty the form without touching approval, so Save is enabled and the
    // untouched approval field is what reaches the payload.
    await userEvent.click(screen.getByRole('radio', { name: 'Expand all' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ modelApprovalAction: null }))
  })

  it('saves the full payload and trims the denylist', async () => {
    mount()
    await userEvent.click(screen.getByRole('radio', { name: 'Block' }))
    await userEvent.type(screen.getByLabelText('Gated models'), ' fable, opus ')
    await userEvent.click(screen.getByRole('radio', { name: 'Expand all' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(mutateAsync).toHaveBeenCalledWith({
      defaultPermissionMode: 'interactive', modelApprovalAction: 'block', modelApprovalDenylist: 'fable, opus',
      defaultToolDetail: 'all-open', thinkingCollapsed: true, repoRoot: null,
    })
  })

  describe('Worktrees (D-14)', () => {
    const COPY = "Where repos are cloned and session worktrees are created. Changing it doesn't move existing worktrees — they keep working but cleanup skips them."

    it('edits the repo root here, with the copy, its server default and a Reset', async () => {
      vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({ repoRoot: null })))
      const { container } = mount()
      const root = screen.getByLabelText('Worktree location')
      expect(root).toHaveAccessibleDescription(COPY)
      expect(root).toHaveAttribute('placeholder', '/home/user/GitHub · server default')
      await userEvent.type(root, ' /srv/repos ')
      await userEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ repoRoot: '/srv/repos' }))
      await userEvent.click(screen.getByRole('button', { name: 'Reset worktree location to default' }))
      expect(root).toHaveValue('')
      expect(await axe(container)).toHaveNoViolations()
    })

    it('Reset on a stored root saves repoRoot: null — the server clears on null', async () => {
      vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({ repoRoot: '/srv/old' })))
      mount()
      expect(screen.getByLabelText('Worktree location')).toHaveValue('/srv/old')
      await userEvent.click(screen.getByRole('button', { name: 'Reset worktree location to default' }))
      await userEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ repoRoot: null }))
    })

    it('edits the branch prefix, with its copy, the built-in placeholder and a Reset (D-22)', async () => {
      const { container } = mount()
      const field = screen.getByRole('textbox', { name: 'Branch prefix' })
      expect(field).toHaveAttribute('placeholder', 'viberpit/')
      expect(field).toHaveAccessibleDescription(BRANCH_PREFIX_COPY)
      await userEvent.type(field, ' agent/ ')
      await userEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ branchPrefix: 'agent/' }))
      expect(await axe(container)).toHaveNoViolations()
    })

    it('saving another field never re-sends an unchanged prefix (D-22)', async () => {
      vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({ branchPrefix: 'agent/' })))
      mount()
      await userEvent.click(screen.getByRole('radio', { name: 'Expand all' }))
      await userEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(mutateAsync).toHaveBeenCalledTimes(1)
      expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty('branchPrefix')
      expect(mutateAsync.mock.calls[0][0]).toMatchObject({ defaultToolDetail: 'all-open' })
    })

    it('Reset on a stored prefix saves branchPrefix: null — back to the built-in', async () => {
      vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({ branchPrefix: 'agent/' })))
      mount()
      const field = screen.getByRole('textbox', { name: 'Branch prefix' })
      expect(field).toHaveValue('agent/')
      await userEvent.click(screen.getByRole('button', { name: 'Reset branch prefix to default' }))
      expect(field).toHaveValue('')
      await userEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ branchPrefix: null }))
    })

    it('shows a refused prefix inline under the field, not in the banner, until the next edit', async () => {
      const reason = 'Branches already use agent/ that ViberPit did not create: repo:agent/mine.'
      const err = new ApiRequestError(400, { detail: { code: 'BRANCH_PREFIX_INVALID', reason } })
      mutateAsync.mockRejectedValueOnce(err)
      vi.mocked(hooks.useUpdateSettings).mockReturnValue(mutationResult({ mutateAsync, error: err }))
      const { container } = mount()
      const field = screen.getByRole('textbox', { name: 'Branch prefix' })
      expect(field).not.toHaveAttribute('aria-invalid')
      await userEvent.type(field, 'agent/')
      await userEvent.click(screen.getByRole('button', { name: 'Save' }))
      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent(reason)
      expect(field).toHaveAttribute('aria-invalid', 'true')
      expect(field).toHaveAccessibleDescription(expect.stringContaining(reason))
      expect(screen.queryByText('Not saved')).toBeNull()
      expect(await axe(container)).toHaveNoViolations()
      await userEvent.type(field, 'x')
      expect(screen.queryByText(reason)).toBeNull()
      expect(field).not.toHaveAttribute('aria-invalid')
    })

    it("takes the placeholder from the server's effectiveDefaults, not a client constant", () => {
      const base = appSettings()
      vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
        effectiveDefaults: { ...base.effectiveDefaults, branchPrefix: 'built-in/' },
      })))
      mount()
      expect(screen.getByRole('textbox', { name: 'Branch prefix' })).toHaveAttribute('placeholder', 'built-in/')
    })

    it('any other save failure still shows in the section banner', async () => {
      const err = new ApiRequestError(422, { detail: 'invalid defaultToolDetail' })
      mutateAsync.mockRejectedValueOnce(err)
      vi.mocked(hooks.useUpdateSettings).mockReturnValue(mutationResult({ mutateAsync, error: err }))
      mount()
      await userEvent.type(screen.getByRole('textbox', { name: 'Branch prefix' }), 'agent/')
      await userEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(await screen.findByText('Not saved')).toBeInTheDocument()
    })
  })

  describe('Sandbox new sessions (D-23)', () => {
    it('is hidden without the capability (the kill switch, or an older server)', () => {
      mount()
      expect(screen.queryByRole('checkbox', { name: 'Sandbox new sessions' })).toBeNull()
      vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({ sandboxCapability: false })))
      mount()
      expect(screen.queryByRole('checkbox', { name: 'Sandbox new sessions' })).toBeNull()
    })

    it('shows off by default with the host requirements, and saves only a change', async () => {
      vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({ sandboxCapability: true })))
      const { container } = mount()
      const box = screen.getByRole('checkbox', { name: 'Sandbox new sessions' })
      expect(box).not.toBeChecked()
      expect(screen.getByText(SANDBOX_DEFAULT_COPY)).toBeInTheDocument()
      expect(SANDBOX_DEFAULT_COPY).toMatch(/Off by default/)
      expect(SANDBOX_DEFAULT_COPY).toMatch(/bwrap/)
      expect(SANDBOX_DEFAULT_COPY).toMatch(/socat/)
      await userEvent.click(box)
      await userEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ sandboxDefault: true }))
      expect(await axe(container)).toHaveNoViolations()
    })

    it('an unrelated save never sends the sandbox default', async () => {
      vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({ sandboxCapability: true, sandboxDefault: true })))
      mount()
      expect(screen.getByRole('checkbox', { name: 'Sandbox new sessions' })).toBeChecked()
      await userEvent.click(screen.getByRole('radio', { name: 'Expand all' }))
      await userEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty('sandboxDefault')
    })
  })
})
