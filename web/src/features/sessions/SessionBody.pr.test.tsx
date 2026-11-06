/**
 * D-15: the session's PR tab. Listed only when the session record has PRs (after
 * Changes, before Files), hidden without — `?view=pr` then falls back to the
 * conversation. The pane itself is lazy (`pr/PrTab`).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { SessionBody } from './SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { EMPTY_BACKGROUND_WORK_VIEW } from '@/session/selectors'
import { setElementWidth, resetElementWidth } from '@/test/elementWidth'

vi.mock('@/features/sessions/pr/PrTab', () => ({
  default: ({ sessionId, prs, hasFiles, onShowConversation, onOpenFile }: {
    sessionId: string; prs: { number: number }[]; hasFiles: boolean
    onShowConversation: () => void; onOpenFile: (p: string, l?: number | null) => void
  }) => (
    <div data-testid="pr-tab-stub" data-files={String(hasFiles)}>
      PR:{sessionId}:{prs.map((p) => p.number).join(',')}
      <button type="button" onClick={onShowConversation}>back</button>
      <button type="button" onClick={() => onOpenFile('src/a.ts', 3)}>file</button>
    </div>
  ),
}))

vi.mock('@/features/sessions/files/FilesTab', () => ({
  default: () => <div data-testid="files-tab-stub" />,
}))

vi.mock('@/features/sessions/transcript/TranscriptView', () => ({
  TranscriptView: () => <div data-testid="transcript" />,
}))

const { sessionsHook, diffHook } = vi.hoisted(() => ({ sessionsHook: vi.fn(), diffHook: vi.fn() }))

// Dev-server presence (D-18) lists the Browser tab; none here.
vi.mock('@/api/devServers', () => ({ useSessionDevServers: () => ({ servers: null, refetch: () => {} }) }))

vi.mock('@/api/hooks', () => ({
  useSessionMcp: vi.fn(() => ({ data: undefined })),
  usePermissionRules: vi.fn(() => ({ data: [] })),
  useMemoryFile: vi.fn(() => ({ data: undefined })),
  useRevokePermissionRule: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useReconnectSessionMcp: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null })),
  useAccounts: vi.fn(() => ({ data: [] })),
  useSessions: sessionsHook,
  useEngineCapabilities: vi.fn(() => undefined),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  useResolveSuspectedComplete: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useMarkSessionViewed: vi.fn(() => ({ mutate: vi.fn() })),
  useSessionDiff: diffHook,
  useForkSession: vi.fn(() => ({ fork: vi.fn(), isPending: false })),
  useRewindSession: vi.fn(() => ({ preview: vi.fn(), rewind: vi.fn() })),
  useUndoRewind: vi.fn(() => ({ undo: vi.fn(), isPending: false })),
  useSetAccountPin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSetPrAutofix: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useStopSubAgent: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

function makeRuntime(): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [{ id: 't1', role: 'assistant', blocks: [] }], openBlockId: null, status: { state: 'idle', count: 0 },
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
  } as unknown as SessionRuntime
}

function LocationProbe() {
  return <output data-testid="location-search">{useLocation().search}</output>
}

function renderBody(path = '/sessions/s1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SessionBody rt={makeRuntime()} />
      <LocationProbe />
    </MemoryRouter>,
  )
}

const PR = { number: 7, url: 'https://github.com/o/n/pull/7', state: 'open', checks: 'passing', title: 'x', deployed: false }
const withPrs = (prs: unknown[], extra: Record<string, unknown> = {}) =>
  sessionsHook.mockReturnValue({ data: [{ id: 's1', accountName: 'acct', status: 'idle', prs, ...extra }] })

beforeEach(() => {
  sessionsHook.mockReset()
  diffHook.mockReturnValue({ data: undefined, refetch: vi.fn() })
  vi.spyOn(global, 'fetch').mockResolvedValue(new Response(JSON.stringify({ enabled: false }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
  setElementWidth(1200)
})
afterEach(() => {
  resetElementWidth()
  vi.restoreAllMocks()
})

describe('SessionBody — PR tab (D-15)', () => {
  it('is hidden without PRs, and ?view=pr falls back to the conversation', () => {
    withPrs([])
    renderBody('/sessions/s1?view=pr')
    expect(screen.queryByRole('tab', { name: 'PR' })).toBeNull()
    expect(screen.queryByTestId('pr-tab-stub')).toBeNull()
    expect(screen.getByTestId('transcript')).toBeInTheDocument()
  })

  it('is listed after Changes and before Files', () => {
    diffHook.mockReturnValue({ data: { files: [{ path: 'a' }] }, refetch: vi.fn() })
    withPrs([PR], { cwd: '/w/x' })
    renderBody()
    const tabs = within(screen.getByRole('tablist')).getAllByRole('tab').map((t) => t.textContent)
    expect(tabs).toEqual(['Conversation', 'Changes', 'PR', 'Files'])
  })

  it('opens on click, and ?view=pr selects it directly', async () => {
    withPrs([PR])
    renderBody()
    fireEvent.click(screen.getByRole('tab', { name: 'PR' }))
    expect(screen.getByTestId('location-search').textContent).toContain('view=pr')
    expect(await screen.findByTestId('pr-tab-stub')).toHaveTextContent('PR:s1:7')
    expect(document.getElementById('tab-panel-pr')).toHaveAttribute('role', 'tabpanel')
  })

  it('?view=pr selects it; the pane can return to the conversation and open a file', async () => {
    withPrs([PR], { cwd: '/w/x' })
    renderBody('/sessions/s1?view=pr')
    expect(screen.getByRole('tab', { name: 'PR' })).toHaveAttribute('aria-selected', 'true')
    const stub = await screen.findByTestId('pr-tab-stub')
    expect(stub).toHaveAttribute('data-files', 'true')
    fireEvent.click(within(stub).getByRole('button', { name: 'file' }))
    // The router commits in a transition, held until the lazy Files pane resolves.
    expect(await screen.findByTestId('files-tab-stub')).toBeInTheDocument()
    const search = new URLSearchParams(screen.getByTestId('location-search').textContent!)
    expect(search.get('view')).toBe('files')
    expect(search.get('file')).toBe('src/a.ts')
  })

  it('the pane\'s "back to conversation" selects the Conversation tab', async () => {
    withPrs([PR])
    renderBody('/sessions/s1?view=pr')
    fireEvent.click(within(await screen.findByTestId('pr-tab-stub')).getByRole('button', { name: 'back' }))
    expect(screen.getByRole('tab', { name: 'Conversation' })).toHaveAttribute('aria-selected', 'true')
  })
})
