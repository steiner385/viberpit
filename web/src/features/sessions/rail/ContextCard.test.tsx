import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import type { McpServerState, UsageWindow } from '@/api/types'
import type { Command, MemoryFile } from '@/session/events'
import { PromptRow } from '../composer/PromptRow'

const { sessionMcp, rules, memoryFile } = vi.hoisted(() => ({ sessionMcp: vi.fn(), rules: vi.fn(), memoryFile: vi.fn() }))
vi.mock('@/api/hooks', () => ({
  useSessionMcp: sessionMcp,
  useReconnectSessionMcp: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useStartMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useCancelMcpAuth: () => ({ mutate: vi.fn(), isPending: false }),
  useSubmitMcpRedirect: () => ({ mutate: vi.fn(), isPending: false }),
  useMcpAuthStatus: () => ({ data: undefined }),
  usePermissionRules: rules,
  useRevokePermissionRule: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useMemoryFile: memoryFile,
}))
vi.mock('@/api/mcpScopeHooks', () => ({
  useToggleSessionMcp: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null, variables: undefined }),
  useSetSessionMcpTool: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null, variables: undefined }),
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
  memoryFiles: null as MemoryFile[] | null,
  cwd: '/home/u/wt' as string | null,
  onCompact: () => {},
}

// CLI order, as the engine reports it: the user file first, then the walk, then the worktree.
const FILES: MemoryFile[] = [
  { path: '/home/u/.claude-accounts/cfg/work/CLAUDE.md', type: 'User', tokens: 1234 },
  { path: '/home/u/wt/CLAUDE.md', type: 'Project', tokens: 958 },
  { path: '/home/u/wt/CLAUDE.local.md', type: 'Local', tokens: 4 },
  { path: '/home/u/.claude-accounts/cfg/work/projects/p/memory/MEMORY.md', type: 'AutoMem', tokens: 80 },
  { path: '/etc/claude-code/CLAUDE.md', type: 'Plugin', tokens: 7 },
]

describe('ContextCard', () => {
  beforeEach(() => {
    sessionMcp.mockReset().mockReturnValue({ data: undefined })
    rules.mockReset().mockReturnValue({ data: [] })
    memoryFile.mockReset().mockReturnValue({ data: undefined, isError: false, error: null, refetch: vi.fn() })
    // jsdom has no Element.scrollTo; the viewer's virtualizer goes through it.
    Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: vi.fn() })
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

  it('offers per-tool switches only with the flag-settings capability (none for Codex)', async () => {
    const { unmount } = render(<ContextCard {...base} mcpLive={[server('github')]} />, { wrapper: Wrapper })
    expect(await screen.findByText('github')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Tools for github' })).toBeNull()
    unmount()
    sessionMcp.mockReturnValue({ data: { servers: [{ ...server('github'), tools: [] }] }, isPending: false, isError: false })
    render(<ContextCard {...base} mcpLive={[server('github')]} mcpToolSwitches />, { wrapper: Wrapper })
    fireEvent.click(await screen.findByRole('button', { name: 'Tools for github' }))
    expect(screen.getByText('No tools.')).toBeInTheDocument()
    expect(sessionMcp).toHaveBeenCalledWith('s1', true, true)
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

  it('titles each quota window with its limit and reset', () => {
    const windows = [
      { key: 'session', label: 'session', pct: 42, resetsAt: '2026-06-19T14:30:00.000Z', durationSeconds: 18000 },
      { key: 'weekly', label: 'weekly', pct: 18, resetsAt: null, durationSeconds: 604800 },
    ] as UsageWindow[]
    render(<ContextCard {...base} quota={windows} />, { wrapper: Wrapper })
    const sess = screen.getByText(/sess 42%/i)
    expect(sess.getAttribute('title')).toMatch(/session limit/i)
    expect(sess.getAttribute('title')).toMatch(/resets/i)
    const wk = screen.getByText(/wk 18%/i)
    expect(wk.getAttribute('title')).toMatch(/weekly limit/i)
    expect(wk.getAttribute('title')).toMatch(/no active window/i)
  })

  it('renders exactly one readout for a single-window provider, and none without quota', () => {
    const { rerender } = render(
      <ContextCard {...base} quota={[{ key: 'premium', label: 'premium-req', pct: 20, resetsAt: null }] as UsageWindow[]} />,
      { wrapper: Wrapper },
    )
    const q = screen.getByRole('list', { name: 'Account quota' })
    expect(q.querySelectorAll('li')).toHaveLength(1)
    expect(q).toHaveTextContent('prem 20%')
    rerender(<ContextCard {...base} quota={null} />)
    expect(screen.queryByRole('list', { name: 'Account quota' })).toBeNull()
  })

  describe('Memory (D-13)', () => {
    it('lists the loaded files in CLI order with label, display path and tokens', () => {
      render(<ContextCard {...base} memoryFiles={FILES} />, { wrapper: Wrapper })
      const list = screen.getByRole('list', { name: 'Memory files' })
      const rows = within(list).getAllByRole('listitem')
      expect(rows.map((r) => r.textContent)).toEqual([
        'User/home/u/.claude-accounts/cfg/work/CLAUDE.md1,234 tokens',
        'ProjectCLAUDE.md958 tokens',
        'LocalCLAUDE.local.md4 tokens',
        'Auto memory/home/u/.claude-accounts/cfg/work/projects/p/memory/MEMORY.md80 tokens',
        // An unknown type is shown as the CLI sent it.
        'Plugin/etc/claude-code/CLAUDE.md7 tokens',
      ])
      // The full path is the row's title, so a truncated one can still be read.
      expect(within(rows[1]).getByText('CLAUDE.md').parentElement).toHaveAttribute('title', '/home/u/wt/CLAUDE.md')
      // A long path is cut from the START, so the file name stays: an RTL box around an LTR span.
      const shown = within(rows[0]).getByText('/home/u/.claude-accounts/cfg/work/CLAUDE.md')
      expect(shown).toHaveAttribute('dir', 'ltr')
      expect(shown.parentElement).toHaveAttribute('dir', 'rtl')
      expect(shown.parentElement).toHaveAttribute('title', '/home/u/.claude-accounts/cfg/work/CLAUDE.md')
    })

    it('shows absolute paths when the worktree is unknown', () => {
      render(<ContextCard {...base} memoryFiles={FILES.slice(1, 2)} cwd={null} />, { wrapper: Wrapper })
      expect(screen.getByRole('button', { name: /Project \/home\/u\/wt\/CLAUDE\.md/ })).toBeInTheDocument()
    })

    it('hides the list when no files are loaded, or the engine reports none', () => {
      const { rerender } = render(<ContextCard {...base} memoryFiles={[]} />, { wrapper: Wrapper })
      expect(screen.queryByRole('list', { name: 'Memory files' })).toBeNull()
      expect(screen.queryByText('Memory')).toBeNull()
      rerender(<ContextCard {...base} memoryFiles={null} />)
      expect(screen.queryByRole('list', { name: 'Memory files' })).toBeNull()
    })

    it('opens a read-only viewer for a file; a project file has no Edit link', async () => {
      memoryFile.mockReturnValue({
        data: { path: FILES[1].path, type: 'Project', text: '# Project rules\nbe kind\n', truncated: false, editable: false },
        isError: false, error: null, refetch: vi.fn(),
      })
      render(<ContextCard {...base} memoryFiles={FILES} />, { wrapper: Wrapper })
      fireEvent.click(screen.getByRole('button', { name: /^Project CLAUDE\.md/ }))
      const dialog = await screen.findByRole('dialog', { name: 'CLAUDE.md' })
      expect(memoryFile).toHaveBeenCalledWith('s1', '/home/u/wt/CLAUDE.md')
      expect(await within(dialog).findByText('be kind')).toBeInTheDocument()
      expect(within(dialog).getByText('Project memory · /home/u/wt/CLAUDE.md')).toBeInTheDocument()
      // Read-only: nothing to type into, and no way to Settings for a project file.
      expect(within(dialog).queryByRole('textbox')).toBeNull()
      expect(within(dialog).queryByRole('link', { name: 'Edit in Settings' })).toBeNull()
      fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
      expect(screen.queryByRole('dialog')).toBeNull()
    })

    it('the user file links to Settings › Memory', async () => {
      memoryFile.mockReturnValue({
        data: { path: FILES[0].path, type: 'User', text: 'always test\n', truncated: true, editable: true },
        isError: false, error: null, refetch: vi.fn(),
      })
      render(<ContextCard {...base} memoryFiles={FILES} />, { wrapper: Wrapper })
      fireEvent.click(screen.getByRole('button', { name: /^User / }))
      const dialog = await screen.findByRole('dialog')
      expect(await within(dialog).findByText('always test')).toBeInTheDocument()
      expect(within(dialog).getByRole('link', { name: 'Edit in Settings' })).toHaveAttribute('href', '/settings/memory')
      expect(within(dialog).getByText('Large file — showing the first 256 KiB.')).toBeInTheDocument()
    })

    it('says so when the file can no longer be read', async () => {
      memoryFile.mockReturnValue({
        data: undefined, isError: true,
        error: new (await import('@/api/client')).ApiRequestError(404, { detail: { code: 'MEMORY_FILE_UNKNOWN' } }),
        refetch: vi.fn(),
      })
      render(<ContextCard {...base} memoryFiles={FILES} />, { wrapper: Wrapper })
      fireEvent.click(screen.getByRole('button', { name: /^Local / }))
      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByText(/no longer loaded or no longer readable/i)).toBeInTheDocument()
    })

    it('a daemon too old to read memory files says so (the shared 409 branch)', async () => {
      const { ApiRequestError } = await import('@/api/client')
      memoryFile.mockReturnValue({
        data: undefined, isError: true,
        error: new ApiRequestError(409, { detail: { code: 'daemon_outdated', message: 'The session daemon is too old for this.' } }),
        refetch: vi.fn(),
      })
      render(<ContextCard {...base} memoryFiles={FILES} />, { wrapper: Wrapper })
      fireEvent.click(screen.getByRole('button', { name: /^Local / }))
      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByText('The session daemon is too old for this.')).toBeInTheDocument()
    })

    it('any other failure offers Retry', async () => {
      const { ApiRequestError } = await import('@/api/client')
      const refetch = vi.fn()
      memoryFile.mockReturnValue({ data: undefined, isError: true, error: new ApiRequestError(500, {}), refetch })
      render(<ContextCard {...base} memoryFiles={FILES} />, { wrapper: Wrapper })
      fireEvent.click(screen.getByRole('button', { name: /^Local / }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' }))
      expect(refetch).toHaveBeenCalled()
    })
  })
})
