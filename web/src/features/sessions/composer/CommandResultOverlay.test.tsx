// web/src/features/sessions/composer/CommandResultOverlay.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import type { ComponentProps } from 'react'
import { CommandResultOverlay } from './CommandResultOverlay'
import type { Command } from '@/session/events'
import type { McpServerState } from '@/api/types'

const startAuth = vi.fn()
vi.mock('@/api/hooks', () => ({
  useReconnectSessionMcp: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useStartMcpAuth: () => ({ mutate: startAuth, isPending: false }),
  useCancelMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useSubmitMcpRedirect: () => ({ mutate: vi.fn(), isPending: false }),
  useMcpAuthStatus: () => ({ data: undefined }),
}))
vi.mock('@/api/mcpScopeHooks', () => ({
  useToggleSessionMcp: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
}))

beforeEach(() => startAuth.mockReset())

function st(name: string, status: string, over: Partial<McpServerState> = {}): McpServerState {
  return { name, status, error: null, toolCount: 2, scope: 'user', ...over }
}

const rt = (mcpServers: McpServerState[] | null = null) =>
  ({ sessionId: 's1', account: 'work', state: { mcpServers } }) as unknown as ComponentProps<typeof CommandResultOverlay>['rt']
const SESSION = { rt: rt() }

const CMDS: Command[] = [{ name: 'help', description: 'List commands', argumentHint: null, kind: 'client', source: 'app' }]

describe('CommandResultOverlay', () => {
  it('renders the help list', () => {
    render(<CommandResultOverlay result={{ render: 'help', data: null }} commands={CMDS} onClose={vi.fn()} {...SESSION} />)
    expect(screen.getByText('help')).toBeInTheDocument()
    expect(screen.getByText('List commands')).toBeInTheDocument()
  })

  it('renders context usage', () => {
    render(<CommandResultOverlay result={{ render: 'context', data: { percentage: 42, totalTokens: 1000, maxTokens: 2000 } }} commands={CMDS} onClose={vi.fn()} {...SESSION} />)
    expect(screen.getByText(/42/)).toBeInTheDocument()
  })

  it('renders nothing when result is null', () => {
    const { container } = render(<CommandResultOverlay result={null} commands={CMDS} onClose={vi.fn()} {...SESSION} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders the /mcp result as the session MCP panel with Reauth for the session account', async () => {
    const data = { servers: [st('plugin:p:aws', 'needs-auth', { auth: 'oauth', error: 'Sign-in expired — re-authenticate.' })] }
    render(
      <MemoryRouter>
        <CommandResultOverlay result={{ render: 'mcp', data }} commands={CMDS} onClose={vi.fn()} {...SESSION} />
      </MemoryRouter>,
    )
    expect(await screen.findByRole('heading', { name: 'MCP servers' })).toBeInTheDocument()
    expect(screen.getByText('aws')).toBeInTheDocument()
    expect(screen.getByText('Sign-in expired — re-authenticate.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reauth aws' }))
    await userEvent.click(screen.getByRole('button', { name: 'Fix auth' }))
    expect(startAuth).toHaveBeenCalledWith({ name: 'aws', account: 'work' })
  })

  it('prefers live MCP state over the /mcp snapshot', async () => {
    const data = { servers: [st('a', 'needs-auth')] }
    render(
      <MemoryRouter>
        <CommandResultOverlay
          result={{ render: 'mcp', data }}
          commands={CMDS}
          onClose={vi.fn()}
          rt={rt([st('a', 'connected')])}
        />
      </MemoryRouter>,
    )
    expect(await screen.findByText('Connected')).toBeInTheDocument()
    expect(screen.queryByText('Needs auth')).toBeNull()
  })

  it('shows an empty state with a link to Settings > MCP', async () => {
    render(
      <MemoryRouter>
        <CommandResultOverlay result={{ render: 'mcp', data: { servers: [] } }} commands={CMDS} onClose={vi.fn()} {...SESSION} />
      </MemoryRouter>,
    )
    expect(await screen.findByText('No MCP servers are configured for this session.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /manage/i })).toHaveAttribute('href', expect.stringContaining('/settings/mcp'))
  })
})
