/**
 * The session rail (D-6): at a pane width of 64rem or more the four cards sit in
 * a right column beside the transcript, collapsible from the toolbar; below it
 * they move into a Details tab, last in the strip. Width is the pane's own
 * (`useContainerMinWidth`), simulated here with `setElementWidth`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { SessionBody } from './SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { EMPTY_BACKGROUND_WORK_VIEW } from '@/session/selectors'
import { setElementWidth, resetElementWidth } from '@/test/elementWidth'
import { RAIL_COLLAPSED_KEY } from './rail/railPreference'

const { diffHook, mcpHook, sessionsHook, prAutofix } = vi.hoisted(() => ({
  diffHook: vi.fn(), mcpHook: vi.fn(), sessionsHook: vi.fn(), prAutofix: vi.fn(),
}))

vi.mock('@/api/hooks', () => ({
  useSessionMcp: mcpHook,
  usePermissionRules: vi.fn(() => ({ data: [] })),
  useMemoryFile: vi.fn(() => ({ data: undefined })),
  useRevokePermissionRule: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useReconnectSessionMcp: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null })),
  useAccounts: vi.fn(() => ({ data: [{ name: 'acct' }] })),
  // No cwd: a cwd lists the Files tab (D-7), and these tests count the strip's tabs.
  useSessions: sessionsHook,
  useEngineCapabilities: vi.fn(() => undefined),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  useResolveSuspectedComplete: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useMarkSessionViewed: vi.fn(() => ({ mutate: vi.fn() })),
  useSessionDiff: diffHook,
  useSetAccountPin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useStopSubAgent: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSetPrAutofix: vi.fn(() => ({ mutate: prAutofix, isPending: false })),
}))

const BASE_SESSION = { id: 's1', accountName: 'acct', status: 'idle', branch: 'feat/x' }

function makeRuntime(over: Partial<SessionRuntime> = {}): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [], openBlockId: null, status: { state: 'idle', count: 0 },
      title: null, model: null, usage: null, replaying: false, error: null,
      queued: [], rotations: [], latestSuggestions: [], subAgents: {}, mcpServers: null,
    },
    suggestions: [], mode: 'interactive', toolDetailMode: 'auto', thinkingCollapsed: true,
    viewMode: 'normal', setViewMode: vi.fn(),
    connectionState: 'connected',
    title: 'Test', account: 'acct', quota: null, contextPct: null, subAgents: [],
    backgroundWork: EMPTY_BACKGROUND_WORK_VIEW, commands: [],
    hasOpenAction: false, armedQuestionId: null, chatArmed: false,
    onDecision: vi.fn(), onAnswer: vi.fn(), onChat: vi.fn(), onPrompt: vi.fn(), onPlainPrompt: vi.fn(),
    onInterrupt: vi.fn(), onSetMode: vi.fn(), onCancelQueued: vi.fn(), reattach: vi.fn(),
    ...over,
  } as unknown as SessionRuntime
}

function LocationProbe() {
  return <output data-testid="location-search">{useLocation().search}</output>
}
const search = () => screen.getByTestId('location-search').textContent

function renderBody(rt = makeRuntime(), path = '/sessions/s1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SessionBody rt={rt} />
      <LocationProbe />
    </MemoryRouter>,
  )
}

const CARDS = ['Changes', 'Context', 'Environment', 'Tasks']

describe('SessionBody — session rail', () => {
  beforeEach(() => {
    window.localStorage.clear()
    diffHook.mockReset().mockReturnValue({ data: undefined, refetch: vi.fn() })
    mcpHook.mockReset().mockReturnValue({ data: undefined })
    sessionsHook.mockReset().mockReturnValue({ data: [BASE_SESSION] })
    prAutofix.mockReset()
  })
  afterEach(() => resetElementWidth())

  describe('PR auto-fix switch (D-14)', () => {
    beforeEach(() => { setElementWidth(1200) })
    const pr = { number: 7, url: 'https://github.com/o/r/pull/7', state: 'open', checks: 'failing', title: 'PR 7', deployed: false }

    it('is absent on a session with no PR', async () => {
      renderBody()
      await screen.findByRole('region', { name: 'Environment' })
      expect(screen.queryByRole('switch', { name: 'PR auto-fix' })).toBeNull()
    })

    it("posts the flipped value for the open session's PRs", async () => {
      sessionsHook.mockReturnValue({ data: [{ ...BASE_SESSION, prs: [pr], prAutofixPaused: false }] })
      renderBody()
      const toggle = await screen.findByRole('switch', { name: 'PR auto-fix' })
      expect(toggle).toHaveAttribute('aria-checked', 'true')
      fireEvent.click(toggle)
      expect(prAutofix).toHaveBeenCalledWith({ id: 's1', enabled: false })
    })

    it('reads a paused session as off', async () => {
      sessionsHook.mockReturnValue({ data: [{ ...BASE_SESSION, prs: [pr], prAutofixPaused: true }] })
      renderBody()
      expect(await screen.findByRole('switch', { name: 'PR auto-fix' })).toHaveAttribute('aria-checked', 'false')
    })
  })

  describe('on a wide pane (≥ 64rem)', () => {
    beforeEach(() => { setElementWidth(1200) })

    it('renders all four cards in the rail and no Details tab', async () => {
      renderBody()
      const rail = screen.getByRole('complementary', { name: 'Session details' })
      // The cards load lazily (off the entry chunk).
      for (const name of CARDS) expect(await within(rail).findByRole('region', { name })).toBeInTheDocument()
      expect(screen.queryByRole('tab', { name: 'Details' })).toBeNull()
      // Conversation alone: no tab strip on a wide pane.
      expect(screen.queryByRole('tablist')).toBeNull()
    })

    it('the toolbar toggle collapses the rail, and the choice persists', () => {
      const { unmount } = renderBody()
      fireEvent.click(screen.getByRole('button', { name: 'Hide details' }))
      expect(screen.queryByRole('complementary', { name: 'Session details' })).toBeNull()
      expect(window.localStorage.getItem(RAIL_COLLAPSED_KEY)).toBe('true')
      unmount()

      renderBody()
      expect(screen.queryByRole('complementary', { name: 'Session details' })).toBeNull()
      const show = screen.getByRole('button', { name: 'Show details' })
      expect(show).toHaveAttribute('aria-expanded', 'false')
      fireEvent.click(show)
      expect(screen.getByRole('complementary', { name: 'Session details' })).toBeInTheDocument()
      expect(window.localStorage.getItem(RAIL_COLLAPSED_KEY)).toBe('false')
    })

    it('?view=details falls back to the conversation', async () => {
      renderBody(makeRuntime(), '/sessions/s1?view=details')
      expect(document.getElementById('tab-panel-details')).toBeNull()
      // The cards are in the rail, once — not also in a Details panel.
      expect(await screen.findAllByRole('region', { name: 'Context' })).toHaveLength(1)
    })

    it('Open Changes in the Changes card opens the Changes tab', async () => {
      const files = [{ path: 'a.ts', oldPath: null, status: 'modified', additions: 1, deletions: 0, binary: false, tooLarge: false, hunks: [] }]
      diffHook.mockReturnValue({ data: { base: 'a', head: 'b', files, truncated: false, additions: 1, deletions: 0 }, refetch: vi.fn() })
      renderBody()
      fireEvent.click(await screen.findByRole('button', { name: /open changes/i }))
      expect(search()).toContain('view=changes')
      expect(screen.getByRole('tab', { name: 'Changes' })).toHaveAttribute('aria-selected', 'true')
    })

    it('Compact now sends /compact as a plain prompt', async () => {
      const rt = makeRuntime({ contextPct: 5, commands: [{ name: 'compact', description: '', argumentHint: null, kind: 'passthrough' }] })
      renderBody(rt)
      fireEvent.click(await screen.findByRole('button', { name: 'Compact now' }))
      expect(rt.onPlainPrompt).toHaveBeenCalledWith('/compact', [])
    })

    it('a collapsed rail toggle carries the attention dot when background work runs', () => {
      window.localStorage.setItem(RAIL_COLLAPSED_KEY, 'true')
      const backgroundWork = { rows: [{ toolUseId: 't1', kind: 'shell' as const, description: 'npm test', status: 'running' as const, live: true }], wakeup: false, wakeupAt: null }
      renderBody(makeRuntime({ backgroundWork }))
      expect(screen.getByRole('button', { name: 'Show details (needs attention)' })).toBeInTheDocument()
      expect(screen.getByTestId('rail-toggle-attention')).toBeInTheDocument()
    })

    it('a collapsed rail toggle reads the MCP snapshot when there is no live status', () => {
      window.localStorage.setItem(RAIL_COLLAPSED_KEY, 'true')
      mcpHook.mockReturnValue({ data: { servers: [{ name: 'gh', status: 'needs-auth', error: null, toolCount: null, scope: null }] } })
      renderBody()
      expect(mcpHook).toHaveBeenCalledWith('s1', true)
      expect(screen.getByRole('button', { name: 'Show details (needs attention)' })).toBeInTheDocument()
    })

    it('feeds the Environment card from the session record', async () => {
      renderBody()
      const env = await screen.findByRole('region', { name: 'Environment' })
      expect(within(env).getByText('feat/x')).toBeInTheDocument()
      expect(within(env).getByLabelText('Switch account')).toHaveValue('acct')
    })
  })

  describe('on a narrow pane (< 64rem)', () => {
    beforeEach(() => { setElementWidth(800) })

    it('has no rail and no rail toggle; a Details tab, last in the strip, lists all four cards', async () => {
      renderBody()
      expect(screen.queryByRole('complementary', { name: 'Session details' })).toBeNull()
      expect(screen.queryByRole('button', { name: /(Show|Hide) details/ })).toBeNull()
      const tabs = within(screen.getByRole('tablist')).getAllByRole('tab').map((t) => t.textContent)
      expect(tabs).toEqual(['Conversation', 'Details'])
      fireEvent.click(screen.getByRole('tab', { name: 'Details' }))
      expect(search()).toContain('view=details')
      const panel = document.getElementById('tab-panel-details')!
      expect(panel).toHaveAttribute('role', 'tabpanel')
      for (const name of CARDS) expect(await within(panel).findByRole('region', { name })).toBeInTheDocument()
    })

    it('the Details tab carries the attention dot when an MCP server has failed', () => {
      const rt = makeRuntime()
      rt.state.mcpServers = [{ name: 'gh', status: 'failed', error: 'ENOENT', toolCount: null, scope: null }]
      renderBody(rt)
      const details = screen.getByRole('tab', { name: 'Details (needs attention)' })
      expect(details.querySelector('.bg-warning')).not.toBeNull()
      // Live status wins: no snapshot fetch.
      expect(mcpHook).toHaveBeenCalledWith('s1', false)
    })

    it('the Details tab has no dot when all is clear', () => {
      renderBody()
      expect(screen.getByRole('tab', { name: 'Details' }).querySelector('.bg-warning')).toBeNull()
    })

    it('?view=details opens the Details tab directly', async () => {
      renderBody(makeRuntime(), '/sessions/s1?view=details')
      expect(screen.getByRole('tab', { name: 'Details' })).toHaveAttribute('aria-selected', 'true')
      expect(await within(document.getElementById('tab-panel-details')!).findByRole('region', { name: 'Tasks' })).toBeInTheDocument()
    })
  })
})
