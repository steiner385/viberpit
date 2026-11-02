import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import type { McpServerState, UsageWindow } from '@/api/types'
import type { Command } from '@/session/events'
import { PromptRow } from '../composer/PromptRow'

const { sessionMcp, rules } = vi.hoisted(() => ({ sessionMcp: vi.fn(), rules: vi.fn() }))
vi.mock('@/api/hooks', () => ({
  useSessionMcp: sessionMcp,
  useReconnectSessionMcp: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useStartMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useCancelMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useSubmitMcpRedirect: () => ({ mutate: vi.fn(), isPending: false }),
  useMcpAuthStatus: () => ({ data: undefined }),
  usePermissionRules: rules,
  useRevokePermissionRule: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))
vi.mock('@/api/mcpScopeHooks', () => ({
  useToggleSessionMcp: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null, variables: undefined }),
}))

import { ContextCard } from './ContextCard'

function Wrapper({ children }: { children: ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

const cmd = (name: string): Command => ({ name, description: '', argumentHint: null, kind: 'passthrough' })
const server = (name: string, status = 'connected'): McpServerState =>
  ({ name, status, error: null, toolCount: 2, scope: 'user' })
const quota: UsageWindow[] = [
  { key: 'session', label: 'session', pct: 42, resetsAt: null, durationSeconds: 18000 },
  { key: 'weekly', label: 'weekly', pct: 18, resetsAt: null, durationSeconds: 604800 },
] as UsageWindow[]

const base = {
  sessionId: 's1',
  account: 'work',
  contextPct: 60 as number | null,
  quota: null as UsageWindow[] | null,
  commands: [cmd('compact')],
  mcpLive: null as McpServerState[] | null,
  onCompact: () => {},
}

describe('ContextCard', () => {
  beforeEach(() => {
    sessionMcp.mockReset().mockReturnValue({ data: undefined })
    rules.mockReset().mockReturnValue({ data: [] })
  })

  it('is a labelled "Context" section', () => {
    render(<ContextCard {...base} />, { wrapper: Wrapper })
    expect(screen.getByRole('region', { name: 'Context' })).toBeInTheDocument()
  })

  it('labels the meter with the same numbers as the prompt-row ring', () => {
    render(<ContextCard {...base} contextPct={63} />, { wrapper: Wrapper })
    const meter = screen.getByRole('meter')
    expect(meter).toHaveAccessibleName('Context 37% used')
    expect(meter).toHaveAttribute('aria-valuenow', '37')
    expect(screen.getByText('Context 37% used')).toBeInTheDocument()

    // The ring, fed the same `rt.contextPct`, reports the same number.
    render(
      <PromptRow rt={{
        mode: 'default', pendingMode: null, onSetMode: () => {}, model: null, liveModel: null, modelPending: false,
        onSetModel: () => {}, contextPct: 63, state: { status: { state: 'idle' } },
      } as never} />,
    )
    expect(screen.getByRole('img', { name: 'Context 37% used' })).toBeInTheDocument()
  })

  it('at danger the meter and the ring carry the same number; only the ring adds the suffix', () => {
    render(<ContextCard {...base} contextPct={8} />, { wrapper: Wrapper })
    expect(screen.getByRole('meter')).toHaveAccessibleName('Context 92% used')
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuenow', '92')
    render(
      <PromptRow rt={{
        mode: 'default', pendingMode: null, onSetMode: () => {}, model: null, liveModel: null, modelPending: false,
        onSetModel: () => {}, contextPct: 8, state: { status: { state: 'idle' } },
      } as never} />,
    )
    expect(screen.getByRole('img', { name: 'Context 92% used · auto-compacts soon' })).toBeInTheDocument()
  })

  it('shows no compact warning outside danger', () => {
    render(<ContextCard {...base} contextPct={20} />, { wrapper: Wrapper })
    expect(screen.queryByText('Auto-compacts soon')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Compact now' })).toBeNull()
  })

  it('at danger shows "Auto-compacts soon" and Compact now calls onCompact', () => {
    const onCompact = vi.fn()
    render(<ContextCard {...base} contextPct={8} onCompact={onCompact} />, { wrapper: Wrapper })
    expect(screen.getByText('Auto-compacts soon')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Compact now' }))
    expect(onCompact).toHaveBeenCalledTimes(1)
  })

  it('hides Compact now when the engine has no compact command', () => {
    render(<ContextCard {...base} contextPct={8} commands={[cmd('clear')]} />, { wrapper: Wrapper })
    expect(screen.getByText('Auto-compacts soon')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Compact now' })).toBeNull()
  })

  it('renders no meter before any usage is known', () => {
    render(<ContextCard {...base} contextPct={null} />, { wrapper: Wrapper })
    expect(screen.queryByRole('meter')).toBeNull()
  })

  it('embeds the MCP server list inline with a Manage link (live servers win)', async () => {
    render(<ContextCard {...base} mcpLive={[server('github'), server('linear', 'failed')]} />, { wrapper: Wrapper })
    expect(await screen.findByText('github')).toBeInTheDocument()
    expect(screen.getByText('linear')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Manage →' })).toHaveAttribute('href', expect.stringContaining('mcp'))
    // No popover trigger: the list is inline.
    expect(screen.queryByRole('button', { name: /^MCP \d/ })).toBeNull()
    expect(sessionMcp).toHaveBeenCalledWith('s1', false)
  })

  it('falls back to the MCP snapshot when there is no live status', async () => {
    sessionMcp.mockReturnValue({ data: { servers: [server('sentry')] } })
    render(<ContextCard {...base} />, { wrapper: Wrapper })
    expect(await screen.findByText('sentry')).toBeInTheDocument()
    expect(sessionMcp).toHaveBeenCalledWith('s1', true)
  })

  it('renders the session rules readout', () => {
    rules.mockReturnValue({ data: [{
      id: 'r1', profileId: 'p', pattern: 'Bash(npm test:*)', scope: 'session', scopeKey: 's1', position: 0,
      createdBy: 'tony', createdVia: 'card', createdAt: '2026-10-11T00:00:00Z', hitCount: 3, lastHitAt: null, description: '',
    }] })
    render(<ContextCard {...base} />, { wrapper: Wrapper })
    expect(screen.getByText('Bash(npm test:*)')).toBeInTheDocument()
  })

  it('renders each quota window', () => {
    render(<ContextCard {...base} quota={quota} />, { wrapper: Wrapper })
    const q = screen.getByRole('list', { name: 'Account quota' })
    expect(q).toHaveTextContent('sess')
    expect(q).toHaveTextContent('42%')
    expect(q).toHaveTextContent('wk')
    expect(q).toHaveTextContent('18%')
    expect(screen.getByLabelText(/session usage: 42%/)).toBeInTheDocument()
  })
})
