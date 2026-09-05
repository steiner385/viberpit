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
import { queryResult, loadingQueryResult, mutationResult } from '@/test/reactQuery'
import { SessionDefaultsSection } from './SessionDefaultsSection'

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

  it('a failed /settings shows an alert naming the failure, not an empty panel', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(
      queryResult(appSettings(), { isError: true, error: new Error('boom') }),
    )
    mount()
    expect(screen.getByRole('heading', { level: 2, name: 'Defaults' })).toBeInTheDocument()
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Settings could not be loaded')
    expect(alert).toHaveTextContent('boom')
  })

  it('with no gated tiers configured the denylist placeholder is just "Server default" (R14a)', () => {
    // Same reason as R13 in GeneralSection: there is no tier to name, and the
    // suffix form would read "Server default · server default".
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      effectiveDefaults: {
        repoRoot: '/home/tony/GitHub', archiveHome: null, modelApprovalAction: 'require_approval',
        modelApprovalDenylist: '', maxActiveSessions: 5,
      },
    })))
    mount()
    expect(screen.getByLabelText('Gated models')).toHaveAttribute('placeholder', 'Server default')
  })

  it('disables, with a reason, any mode the server does not accept (DEC-4)', () => {
    mount()
    const opt = screen.getByRole('option', { name: /Bypass all permissions/ })
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
      defaultToolDetail: 'all-open', thinkingCollapsed: true,
    })
  })
})
