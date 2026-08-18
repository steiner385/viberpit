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
