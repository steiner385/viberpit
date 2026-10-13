import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { McpServerList } from './McpServerList'
import type { McpServerState } from '@/api/types'
import { ApiRequestError } from '@/api/client'

const reconnect = vi.fn()
const toggle = vi.fn()
const startAuth = vi.fn()

vi.mock('@/api/hooks', () => ({
  useReconnectSessionMcp: () => reconnect(),
  useStartMcpAuth: () => ({ mutate: startAuth, isPending: false }),
  useCancelMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useSubmitMcpRedirect: () => ({ mutate: vi.fn(), isPending: false }),
  useMcpAuthStatus: () => ({ data: undefined }),
}))

vi.mock('@/api/mcpScopeHooks', () => ({
  useToggleSessionMcp: () => toggle(),
}))

function st(name: string, status: string, over: Partial<McpServerState> = {}): McpServerState {
  return { name, status, error: null, toolCount: 3, scope: 'user', ...over }
}

beforeEach(() => {
  reconnect.mockReset()
  toggle.mockReset()
  startAuth.mockReset()
  reconnect.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: false, error: null })
  toggle.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: false, error: null })
})

const ui = (servers: McpServerState[]) => <McpServerList sessionId="s1" account="work" servers={servers} />

describe('McpServerList', () => {
  it('renders name, status, tool count and error per server', () => {
    render(ui([st('plugin:p:b', 'needs-auth', { auth: 'oauth', error: 'Sign-in expired — re-authenticate.', toolCount: 5 })]))
    expect(screen.getByText('b')).toBeInTheDocument()
    expect(screen.getByText('Needs auth')).toBeInTheDocument()
    expect(screen.getByText('5 tools')).toBeInTheDocument()
    expect(screen.getByText('Sign-in expired — re-authenticate.')).toBeInTheDocument()
  })

  it('offers Reauth on a needs-auth server', () => {
    render(ui([st('a', 'needs-auth')]))
    expect(screen.getByRole('button', { name: 'Reauth a' })).toHaveTextContent(/^Reauth$/)
  })

  it('offers Re-authenticate on a connected OAuth server', () => {
    render(ui([st('a', 'connected', { auth: 'oauth' })]))
    expect(screen.getByRole('button', { name: 'Re-authenticate a' })).toHaveTextContent(/^Re-authenticate$/)
    expect(screen.queryByRole('button', { name: /Reconnect/ })).toBeNull()
  })

  it('offers no reauth on a non-OAuth healthy server, a missing auth field, or a disabled server', () => {
    render(
      ui([
        st('plain', 'connected', { auth: null }),
        st('legacy', 'connected'),
        st('off', 'disabled', { auth: 'oauth' }),
      ]),
    )
    expect(screen.queryByRole('button', { name: /Reauth|Re-authenticate/ })).toBeNull()
  })

  it('Reauth opens the auth flow for the session account', async () => {
    render(ui([st('plugin:p:b', 'needs-auth', { auth: 'oauth' })]))
    const btn = screen.getByRole('button', { name: 'Reauth b' })
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    await userEvent.click(screen.getByRole('button', { name: 'Fix auth' }))
    expect(startAuth).toHaveBeenCalledWith({ name: 'b', account: 'work' })
  })

  it('Reconnect sends the full server name and shows failures inline', async () => {
    const mutate = vi.fn()
    reconnect.mockReturnValue({
      mutate,
      isPending: false,
      isError: true,
      error: new ApiRequestError(409, { detail: { code: 'MCP_RECONNECT_FAILED', message: 'boom' } }),
    })
    render(ui([st('plugin:p:b', 'failed')]))
    await userEvent.click(screen.getByRole('button', { name: 'Reconnect b' }))
    expect(mutate).toHaveBeenCalledWith({ server: 'plugin:p:b' })
    expect(screen.getByRole('alert')).toHaveTextContent('boom')
  })
})
