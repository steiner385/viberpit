import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { McpStatusReadout } from './McpStatusReadout'
import type { McpServerState } from '@/api/types'
import { ApiRequestError } from '@/api/client'

const sessionMcp = vi.fn()
const reconnect = vi.fn()

vi.mock('@/api/hooks', () => ({
  useSessionMcp: (...a: unknown[]) => sessionMcp(...a),
  useReconnectSessionMcp: () => reconnect(),
  useStartMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useCancelMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useSubmitMcpRedirect: () => ({ mutate: vi.fn(), isPending: false }),
  useMcpAuthStatus: () => ({ data: undefined }),
}))

function Wrapper({ children }: { children: ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

function st(name: string, status: string, error: string | null = null, toolCount: number | null = 3): McpServerState {
  return { name, status, error, toolCount, scope: null }
}

function mockSessionMcp(data: { servers: McpServerState[] } | undefined) {
  sessionMcp.mockReturnValue({ data })
}

function mockReconnect(over: Record<string, unknown> = {}) {
  const rc = { mutate: vi.fn(), isPending: false, isError: false, error: null, ...over }
  reconnect.mockReturnValue(rc)
  return rc
}

const ui = (live: McpServerState[] | null) => (
  <McpStatusReadout sessionId="s1" account="work" live={live} />
)

beforeEach(() => {
  sessionMcp.mockReset()
  reconnect.mockReset()
  mockSessionMcp(undefined)
  mockReconnect()
})

describe('McpStatusReadout', () => {
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
    mockSessionMcp({ servers: [st('a', 'connected')] })
    render(ui(null), { wrapper: Wrapper })
    expect(sessionMcp).toHaveBeenCalledWith('s1', true)
    expect(screen.getByRole('button', { name: /MCP 1\/1/ })).toBeInTheDocument()
  })

  it('prefers live event data over the fetched snapshot and warns on problems', () => {
    mockSessionMcp({ servers: [st('a', 'connected')] })
    render(ui([st('a', 'connected'), st('b', 'needs-auth')]), { wrapper: Wrapper })
    expect(screen.getByRole('button', { name: /MCP 1\/2/ }).className).toMatch(/warning/)
  })

  it('is muted when everything is healthy', () => {
    render(ui([st('a', 'connected')]), { wrapper: Wrapper })
    expect(screen.getByRole('button', { name: /MCP 1\/1/ }).className).not.toMatch(/warning/)
  })

  it('lists error and tool count, and Reconnect calls the endpoint with the full name', async () => {
    const rc = mockReconnect()
    render(ui([st('plugin:p:b', 'failed', 'ENOENT', 5)]), { wrapper: Wrapper })
    await userEvent.click(screen.getByRole('button', { name: /MCP 0\/1/ }))
    expect(screen.getByText('ENOENT')).toBeInTheDocument()
    expect(screen.getByText('5 tools')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reconnect b' }))
    expect(rc.mutate).toHaveBeenCalledWith({ server: 'plugin:p:b' })
  })

  it('hides Reconnect for connected servers', async () => {
    render(ui([st('a', 'connected')]), { wrapper: Wrapper })
    await userEvent.click(screen.getByRole('button', { name: /MCP 1\/1/ }))
    expect(screen.queryByRole('button', { name: /Reconnect/ })).toBeNull()
  })

  it('shows the reconnect failure message inline', async () => {
    mockReconnect({
      isError: true,
      error: new ApiRequestError(409, { detail: { code: 'MCP_RECONNECT_FAILED', message: 'boom' } }),
    })
    render(ui([st('b', 'failed')]), { wrapper: Wrapper })
    await userEvent.click(screen.getByRole('button', { name: /MCP 0\/1/ }))
    expect(screen.getByRole('alert')).toHaveTextContent('boom')
  })

  it('offers Reauth for needs-auth servers', async () => {
    render(ui([st('plugin:p:b', 'needs-auth')]), { wrapper: Wrapper })
    await userEvent.click(screen.getByRole('button', { name: /MCP 0\/1/ }))
    await userEvent.click(screen.getByRole('button', { name: /^Reauth b$/ }))
    expect(screen.getByRole('button', { name: 'Fix auth' })).toBeInTheDocument()
  })

  it('links to Settings > MCP', async () => {
    render(ui([st('a', 'connected')]), { wrapper: Wrapper })
    await userEvent.click(screen.getByRole('button', { name: /MCP 1\/1/ }))
    expect(screen.getByRole('link', { name: /manage/i })).toHaveAttribute('href', expect.stringContaining('/settings/mcp'))
  })
})
