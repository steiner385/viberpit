import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { McpServerList } from './McpServerList'
import type { McpServerState, McpTool } from '@/api/types'
import { ApiRequestError } from '@/api/client'

const reconnect = vi.fn()
const toggle = vi.fn()
const startAuth = vi.fn()
const sessionMcp = vi.fn()
const setTool = vi.fn()

vi.mock('@/api/hooks', () => ({
  useReconnectSessionMcp: () => reconnect(),
  useSessionMcp: (...args: unknown[]) => sessionMcp(...args),
  useStartMcpAuth: () => ({ mutate: startAuth, isPending: false }),
  useCancelMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useSubmitMcpRedirect: () => ({ mutate: vi.fn(), isPending: false }),
  useMcpAuthStatus: () => ({ data: undefined }),
}))

vi.mock('@/api/mcpScopeHooks', () => ({
  useToggleSessionMcp: () => toggle(),
  useSetSessionMcpTool: () => setTool(),
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
  sessionMcp.mockReset()
  setTool.mockReset()
  setTool.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: false, error: null, variables: undefined })
})

function tool(name: string, over: Partial<McpTool> = {}): McpTool {
  return {
    name, description: null, enabled: true, source: 'default',
    annotations: { readOnly: null, destructive: null, openWorld: null }, ...over,
  }
}

/** The session MCP GET as the tool panel reads it. */
function snapshot(servers: McpServerState[]) {
  sessionMcp.mockReturnValue({ data: { servers }, isPending: false, isError: false, error: null })
}

const tools = (servers: McpServerState[], toolSwitches = true) =>
  <McpServerList sessionId="s1" account="work" servers={servers} toolSwitches={toolSwitches} />

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

  describe('tool switches (D-16b)', () => {
    it('a connected server expands to switches read fresh from the session GET', async () => {
      // The live mcpStatus row carries no tools; the GET does.
      snapshot([st('plugin:p:b', 'connected', { tools: [tool('read_page', { enabled: true }), tool('drop_db', { enabled: false })] })])
      render(tools([st('plugin:p:b', 'connected')]))
      const btn = screen.getByRole('button', { name: 'Tools for b' })
      expect(btn).toHaveAttribute('aria-expanded', 'false')
      expect(sessionMcp).not.toHaveBeenCalled()
      await userEvent.click(btn)
      expect(btn).toHaveAttribute('aria-expanded', 'true')
      expect(sessionMcp).toHaveBeenCalledWith('s1', true, true)
      expect(screen.getByRole('switch', { name: 'read_page enabled' })).toHaveAttribute('aria-checked', 'true')
      expect(screen.getByRole('switch', { name: 'drop_db enabled' })).toHaveAttribute('aria-checked', 'false')
    })

    it('toggling posts the full server key and the tool, waiting for the server', async () => {
      const mutate = vi.fn()
      setTool.mockReturnValue({ mutate, isPending: false, isError: false, error: null, variables: undefined })
      snapshot([st('plugin:p:b', 'connected', { tools: [tool('drop_db', { enabled: true })] })])
      render(tools([st('plugin:p:b', 'connected')]))
      await userEvent.click(screen.getByRole('button', { name: 'Tools for b' }))
      await userEvent.click(screen.getByRole('switch', { name: 'drop_db enabled' }))
      expect(mutate).toHaveBeenCalledWith({ server: 'plugin:p:b', tool: 'drop_db', enabled: false })
      // Not optimistic: the switch still shows the server's state.
      expect(screen.getByRole('switch', { name: 'drop_db enabled' })).toHaveAttribute('aria-checked', 'true')
    })

    it('a pending toggle disables the switches; a refusal shows the server text', async () => {
      setTool.mockReturnValue({ mutate: vi.fn(), isPending: true, isError: false, error: null, variables: { tool: 'a' } })
      snapshot([st('s', 'connected', { tools: [tool('a'), tool('b')] })])
      const { unmount } = render(tools([st('s', 'connected')]))
      await userEvent.click(screen.getByRole('button', { name: 'Tools for s' }))
      expect(screen.getByRole('switch', { name: 'a enabled' })).toBeDisabled()
      expect(screen.getByRole('switch', { name: 'b enabled' })).toBeDisabled()
      expect(screen.getByText('Saving…')).toBeInTheDocument()
      unmount()

      setTool.mockReturnValue({
        mutate: vi.fn(), isPending: false, isError: true, variables: undefined,
        error: new ApiRequestError(409, { detail: { code: 'SETTINGS_FAILED', message: 'The CLI did not answer the settings write.' } }),
      })
      render(tools([st('s', 'connected')]))
      await userEvent.click(screen.getByRole('button', { name: 'Tools for s' }))
      expect(screen.getByRole('alert')).toHaveTextContent('The CLI did not answer the settings write.')
      expect(screen.getByRole('switch', { name: 'a enabled' })).toHaveAttribute('aria-checked', 'true')
    })

    it('the human-only refusal is shown as returned', async () => {
      setTool.mockReturnValue({
        mutate: vi.fn(), isPending: false, isError: true, variables: undefined,
        error: new ApiRequestError(403, { detail: { code: 'SESSION_CONTROL_HUMAN_ONLY', message: 'Only a person can change session controls.' } }),
      })
      snapshot([st('s', 'connected', { tools: [tool('a')] })])
      render(tools([st('s', 'connected')]))
      await userEvent.click(screen.getByRole('button', { name: 'Tools for s' }))
      expect(screen.getByRole('alert')).toHaveTextContent('Only a person can change session controls.')
    })

    it('a session override shows the Session chip; annotations show badges; descriptions truncate with a title', async () => {
      const long = Array(10).fill('Deletes the whole database.').join(' ')
      snapshot([st('s', 'connected', { tools: [
        tool('peek', { source: 'session', annotations: { readOnly: true, destructive: false, openWorld: null } }),
        tool('drop', { description: long, annotations: { readOnly: false, destructive: true, openWorld: true } }),
      ] })])
      render(tools([st('s', 'connected')]))
      await userEvent.click(screen.getByRole('button', { name: 'Tools for s' }))
      const rows = screen.getByRole('list', { name: 's tools' }).querySelectorAll('li')
      expect(rows[0]).toHaveTextContent('Session')
      expect(rows[0]).toHaveTextContent('read-only')
      expect(rows[0]).not.toHaveTextContent('destructive')
      expect(rows[1]).not.toHaveTextContent('Session')
      expect(rows[1]).toHaveTextContent('destructive')
      expect(screen.getByText('destructive')).toHaveClass('text-warning')
      const desc = screen.getByTitle(long)
      expect(desc).toHaveClass('truncate')
    })

    it('tools: null (toolCount-only status) shows "Tool list unavailable" and no switches', async () => {
      snapshot([st('s', 'connected', { tools: null })])
      render(tools([st('s', 'connected')]))
      await userEvent.click(screen.getByRole('button', { name: 'Tools for s' }))
      expect(screen.getByText('Tool list unavailable')).toBeInTheDocument()
      // Only the server's own switch remains.
      expect(screen.getAllByRole('switch').map((e) => e.getAttribute('aria-label'))).toEqual(['s enabled'])
      expect(screen.queryByRole('alert')).toBeNull()
    })

    it('no tool switches without the capability (Codex) or on a server that is not connected', () => {
      render(tools([st('s', 'connected')], false))
      expect(screen.queryByRole('button', { name: /^Tools for/ })).toBeNull()
      expect(sessionMcp).not.toHaveBeenCalled()
    })

    it('no Tools on a disabled, pending or failed server', () => {
      render(tools([st('a', 'disabled'), st('b', 'pending'), st('c', 'failed')]))
      expect(screen.queryByRole('button', { name: /^Tools for/ })).toBeNull()
    })

    it('toolsEnforced: false warns on the server once', async () => {
      snapshot([st('s', 'connected', { toolsEnforced: false, tools: [tool('a')] })])
      render(tools([st('s', 'connected', { toolsEnforced: false })]))
      const note = 'Tool settings not applied — restart the session'
      expect(screen.getByText(note)).toHaveClass('text-warning')
      await userEvent.click(screen.getByRole('button', { name: 'Tools for s' }))
      expect(screen.getAllByText(note)).toHaveLength(1)
    })

    it('the live row lacks toolsEnforced; the expanded GET still warns', async () => {
      snapshot([st('s', 'connected', { toolsEnforced: false, tools: [tool('a')] })])
      render(tools([st('s', 'connected')]))
      expect(screen.queryByText(/Tool settings not applied/)).toBeNull()
      await userEvent.click(screen.getByRole('button', { name: 'Tools for s' }))
      expect(screen.getByText('Tool settings not applied — restart the session')).toBeInTheDocument()
    })
  })
})
