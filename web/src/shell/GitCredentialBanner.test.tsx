import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { GitCredentialBanner } from './GitCredentialBanner'
import * as hook from '@/hooks/useCredentialAlerts'
import type { CredentialAlert } from '@/hooks/useCredentialAlerts'

afterEach(() => vi.restoreAllMocks())

const ALERT: CredentialAlert = {
  repo: 'cairnea/KinDash',
  since: 1,
  error: 'Invalid username or token',
  settingsUrl: 'https://github.com/settings/tokens',
  viaApp: false,
}

// The same repo failing while authenticated with a GitHub App installation
// token — refreshing a personal access token would fix nothing here.
const APP_ALERT: CredentialAlert = {
  ...ALERT,
  error: 'remote: Forbidden',
  settingsUrl: 'https://github.com/settings/installations',
  viaApp: true,
}

function mockAlerts(alerts: CredentialAlert[], mutate = vi.fn()) {
  vi.spyOn(hook, 'useCredentialAlerts').mockReturnValue({
    data: alerts,
  } as unknown as ReturnType<typeof hook.useCredentialAlerts>)
  vi.spyOn(hook, 'useDismissCredentialAlert').mockReturnValue({
    mutate,
  } as unknown as ReturnType<typeof hook.useDismissCredentialAlert>)
}

describe('GitCredentialBanner', () => {
  it('renders nothing when there are no alerts', () => {
    mockAlerts([])
    const { container } = render(<GitCredentialBanner />)
    expect(container).toBeEmptyDOMElement()
  })

  it('surfaces the repo and a token-refresh link when a credential is stale', () => {
    mockAlerts([ALERT])
    render(<GitCredentialBanner />)
    expect(screen.getByRole('alert')).toHaveTextContent(/cairnea\/KinDash/)
    const link = screen.getByRole('link', { name: /refresh token/i })
    expect(link).toHaveAttribute('href', 'https://github.com/settings/tokens')
  })

  it('renders in normal flow, never as a fixed overlay', () => {
    // It lives in the shell's BannerStack. A `fixed top-0` copy sat on the same
    // band as the toaster and the page header; in flow it pushes content down.
    mockAlerts([ALERT])
    render(<GitCredentialBanner />)
    const wrapper = screen.getByTestId('git-credential-banners')
    expect(wrapper.className).not.toMatch(/\bfixed\b/)
    expect(wrapper.className).not.toMatch(/\bz-\S+/)
    expect(screen.getByRole('alert').className).not.toMatch(/\bfixed\b/)
  })

  it('points an App-backed failure at the installation, not the token page', () => {
    // Sending the user to Personal Access Tokens for an App-auth failure is a
    // dead end — there is no PAT in play to refresh.
    mockAlerts([APP_ALERT])
    render(<GitCredentialBanner />)
    const link = screen.getByRole('link', { name: /review app access/i })
    expect(link).toHaveAttribute('href', 'https://github.com/settings/installations')
    expect(screen.queryByRole('link', { name: /refresh token/i })).toBeNull()
  })

  it('names the App, not a credential refresh, in an App-backed message', () => {
    mockAlerts([APP_ALERT])
    render(<GitCredentialBanner />)
    expect(screen.getByRole('alert')).toHaveTextContent(/GitHub App access/i)
  })

  it('dismiss hides the row and calls the dismiss mutation', () => {
    const mutate = vi.fn()
    mockAlerts([ALERT], mutate)
    render(<GitCredentialBanner />)
    fireEvent.click(screen.getByRole('button', { name: /dismiss credential alert/i }))
    expect(mutate).toHaveBeenCalledWith('cairnea/KinDash')
    // The row is hidden client-side after dismissal.
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
