/**
 * D-7: the session's Files tab. Listed whenever the session record has a `cwd`
 * (after Changes, before Browser), hidden without one — `?view=files` then falls
 * back to the conversation. The pane itself is lazy (`files/FilesTab`).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { SessionBody } from './SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { EMPTY_BACKGROUND_WORK_VIEW } from '@/session/selectors'
import { setElementWidth, resetElementWidth } from '@/test/elementWidth'

vi.mock('@/features/sessions/files/FilesTab', () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid="files-tab-stub">Files:{sessionId}</div>,
}))

const { sessionsHook } = vi.hoisted(() => ({ sessionsHook: vi.fn() }))

vi.mock('@/api/hooks', () => ({
  useSessionMcp: vi.fn(() => ({ data: undefined })),
  usePermissionRules: vi.fn(() => ({ data: [] })),
  useRevokePermissionRule: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useReconnectSessionMcp: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null })),
  useAccounts: vi.fn(() => ({ data: [] })),
  useSessions: sessionsHook,
  useEngineCapabilities: vi.fn(() => undefined),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  useResolveSuspectedComplete: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSessionDiff: vi.fn(() => ({ data: undefined, refetch: vi.fn() })),
  useSetAccountPin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useStopSubAgent: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

function makeRuntime(): SessionRuntime {
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

const withCwd = (cwd: string | null | undefined) =>
  sessionsHook.mockReturnValue({ data: [{ id: 's1', accountName: 'acct', status: 'idle', cwd }] })

beforeEach(() => {
  sessionsHook.mockReset()
  vi.spyOn(global, 'fetch').mockResolvedValue(new Response(JSON.stringify({ enabled: false }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
  setElementWidth(1200)
})
afterEach(() => {
  resetElementWidth()
  vi.restoreAllMocks()
})

describe('SessionBody — Files tab', () => {
  it('is hidden without a cwd, and ?view=files falls back to the conversation', () => {
    withCwd(null)
    renderBody('/sessions/s1?view=files')
    expect(screen.queryByRole('tab', { name: 'Files' })).toBeNull()
    expect(screen.queryByTestId('files-tab-stub')).toBeNull()
  })

  it('is listed with a cwd, and opens the Files pane', async () => {
    withCwd('/w/x')
    renderBody()
    const tabs = within(screen.getByRole('tablist')).getAllByRole('tab').map((t) => t.textContent)
    expect(tabs).toEqual(['Conversation', 'Files'])
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }))
    expect(screen.getByTestId('location-search').textContent).toContain('view=files')
    expect(await screen.findByTestId('files-tab-stub')).toHaveTextContent('Files:s1')
    expect(document.getElementById('tab-panel-files')).toHaveAttribute('role', 'tabpanel')
  })

  it('?view=files opens it directly', async () => {
    withCwd('/w/x')
    renderBody('/sessions/s1?view=files&file=a.ts&line=4')
    expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByTestId('files-tab-stub')).toBeInTheDocument()
  })
})
