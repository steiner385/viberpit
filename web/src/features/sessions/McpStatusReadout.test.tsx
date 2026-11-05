import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { McpInlineList } from './McpStatusReadout'
import type { McpServerState } from '@/api/types'
import { ApiRequestError } from '@/api/client'

const sessionMcp = vi.fn()
const reconnect = vi.fn()
const toggle = vi.fn()

vi.mock('@/api/hooks', () => ({
  useSessionMcp: (...a: unknown[]) => sessionMcp(...a),
  useReconnectSessionMcp: () => reconnect(),
  useStartMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useCancelMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useSubmitMcpRedirect: () => ({ mutate: vi.fn(), isPending: false }),
  useMcpAuthStatus: () => ({ data: undefined }),
}))

vi.mock('@/api/mcpScopeHooks', () => ({
  useToggleSessionMcp: () => toggle(),
  useSetSessionMcpTool: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
}))

function Wrapper({ children }: { children: ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

function st(
  name: string,
  status: string,
  error: string | null = null,
  toolCount: number | null = 3,
  scope: string | null = 'user',
): McpServerState {
  return { name, status, error, toolCount, scope }
}

function mockSessionMcp(data: { servers: McpServerState[] } | undefined) {
  sessionMcp.mockReturnValue({ data })
}

function mockReconnect(over: Record<string, unknown> = {}) {
  const rc = { mutate: vi.fn(), isPending: false, isError: false, error: null, ...over }
  reconnect.mockReturnValue(rc)
  return rc
}

function mockToggle(over: Record<string, unknown> = {}) {
  const t = { mutate: vi.fn(), isPending: false, isError: false, error: null, variables: undefined, ...over }
  toggle.mockReturnValue(t)
  return t
}

const ui = (live: McpServerState[] | null) => (
  <McpInlineList sessionId="s1" account="work" live={live} />
)

beforeEach(() => {
  sessionMcp.mockReset()
  reconnect.mockReset()
  toggle.mockReset()
  mockSessionMcp(undefined)
  mockReconnect()
  mockToggle()
})

// The session rail's inline MCP list (D-6). The strip's `MCP n/m` popover went
// with `SessionMeta`; the list body (McpServerList + Manage) is unchanged.
describe('McpInlineList', () => {
  it('renders nothing with no servers', () => {
    mockSessionMcp({ servers: [] })
    const { container } = render(ui(null), { wrapper: Wrapper })
    expect(container).toBeEmptyDOMElement()
  })

  it('only queries when there is no live data', () => {
    render(ui([st('a', 'connected')]), { wrapper: Wrapper })
    expect(sessionMcp).toHaveBeenCalledWith('s1', false)
  })

  it('falls back to the fetched snapshot', () => {
    mockSessionMcp({ servers: [st('snap', 'connected')] })
    render(ui(null), { wrapper: Wrapper })
    expect(sessionMcp).toHaveBeenCalledWith('s1', true)
    expect(screen.getByRole('group', { name: 'MCP servers' })).toHaveTextContent('snap')
  })

  it('prefers live event data over the fetched snapshot', () => {
    mockSessionMcp({ servers: [st('snap', 'connected')] })
    render(ui([st('live', 'connected')]), { wrapper: Wrapper })
    const group = screen.getByRole('group', { name: 'MCP servers' })
    expect(group).toHaveTextContent('live')
    expect(group).not.toHaveTextContent('snap')
  })

  it('lists error and tool count, and Reconnect calls the endpoint with the full name', async () => {
    const rc = mockReconnect()
    render(ui([st('plugin:p:b', 'failed', 'ENOENT', 5)]), { wrapper: Wrapper })
    expect(screen.getByText('ENOENT')).toBeInTheDocument()
    expect(screen.getByText('5 tools')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reconnect b' }))
    expect(rc.mutate).toHaveBeenCalledWith({ server: 'plugin:p:b' })
  })

  it('hides Reconnect for connected servers', () => {
    render(ui([st('a', 'connected')]), { wrapper: Wrapper })
    expect(screen.queryByRole('button', { name: /Reconnect/ })).toBeNull()
  })

  it('shows the reconnect failure message inline', () => {
    mockReconnect({
      isError: true,
      error: new ApiRequestError(409, { detail: { code: 'MCP_RECONNECT_FAILED', message: 'boom' } }),
    })
    render(ui([st('b', 'failed')]), { wrapper: Wrapper })
    expect(screen.getByRole('alert')).toHaveTextContent('boom')
  })

  it('offers Reauth for needs-auth servers', async () => {
    render(ui([st('plugin:p:b', 'needs-auth')]), { wrapper: Wrapper })
    await userEvent.click(screen.getByRole('button', { name: /^Reauth b$/ }))
    expect(screen.getByRole('button', { name: 'Fix auth' })).toBeInTheDocument()
  })

  it('links to Settings > MCP', () => {
    render(ui([st('a', 'connected')]), { wrapper: Wrapper })
    expect(screen.getByRole('link', { name: /manage/i })).toHaveAttribute('href', expect.stringContaining('/settings/mcp'))
  })

  it('shows a 404 string detail from the server', () => {
    mockReconnect({ isError: true, error: new ApiRequestError(404, { detail: 'Session not live' }) })
    render(ui([st('b', 'failed')]), { wrapper: Wrapper })
    expect(screen.getByRole('alert')).toHaveTextContent('Session not live')
  })

  it('renders a switch only for user and dynamic (plugin) scope rows', () => {
    render(
      ui([
        st('u', 'connected', null, 3, 'user'),
        st('d', 'connected', null, 3, 'dynamic'),
        st('p', 'connected', null, 3, 'project'),
        st('c', 'connected', null, 3, 'claudeai'),
        st('l', 'connected', null, 3, 'local'),
      ]),
      { wrapper: Wrapper },
    )
    expect(screen.getByRole('switch', { name: 'u enabled' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'd enabled' })).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'p enabled' })).toBeNull()
    expect(screen.queryByRole('switch', { name: 'c enabled' })).toBeNull()
    expect(screen.queryByRole('switch', { name: 'l enabled' })).toBeNull()
  })

  it('switch state follows status', () => {
    render(ui([st('a', 'connected'), st('b', 'disabled')]), { wrapper: Wrapper })
    expect(screen.getByRole('switch', { name: 'a enabled' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('switch', { name: 'b enabled' })).toHaveAttribute('aria-checked', 'false')
  })

  it('toggling sends the full server name', async () => {
    const t = mockToggle()
    render(ui([st('plugin:p:b', 'connected')]), { wrapper: Wrapper })
    await userEvent.click(screen.getByRole('switch', { name: 'b enabled' }))
    expect(t.mutate).toHaveBeenCalledWith({ server: 'plugin:p:b', enabled: false })
  })

  it('shows the toggle failure message inline', () => {
    mockToggle({
      isError: true,
      error: new ApiRequestError(409, { detail: { code: 'MCP_TOGGLE_FAILED', message: 'nope' } }),
    })
    render(ui([st('a', 'connected')]), { wrapper: Wrapper })
    expect(screen.getByRole('alert')).toHaveTextContent('nope')
  })

  it('hides switches after a 404 mcp_scope_disabled', () => {
    mockToggle({ isError: true, error: new ApiRequestError(404, { error: 'mcp_scope_disabled' }) })
    render(ui([st('a', 'connected')]), { wrapper: Wrapper })
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
