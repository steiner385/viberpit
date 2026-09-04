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
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as hooks from '@/api/hooks'
import { appSettings } from '@/test/factories'
import { queryResult, mutationResult } from '@/test/reactQuery'
import { SessionDefaultsSection } from './SessionDefaultsSection'

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

  it('initialises from the server (thinkingCollapsed true, not a hard-coded false) (S21)', () => {
    mount()
    expect(screen.getByRole('checkbox', { name: 'Collapse thinking by default' })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Server default/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Server default/ })).toHaveAccessibleName(/require approval/i)
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

  it('saves model approval as null for server default and trims the denylist', async () => {
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
