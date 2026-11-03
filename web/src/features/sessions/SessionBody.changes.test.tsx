/**
 * D-4: the session's Changes tab. Listed (after Conversation, before Browser)
 * only when the diff summary has files; `?view=changes` selects it; the summary
 * is refetched when a turn ends (`busy` goes false) and when the tab is opened —
 * not when `?view=changes` resolves on load (that is the mount fetch: one request).
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { queryResult } from '@/test/reactQuery'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { EMPTY_BACKGROUND_WORK_VIEW } from '@/session/selectors'
import { setElementWidth, resetElementWidth } from '@/test/elementWidth'
import type { SessionDiff } from './diff/types'
import { SessionBody } from './SessionBody'

vi.mock('@/features/sessions/diff/ChangesTab', () => ({
  default: ({ sessionId, onSendReview }: { sessionId: string; onSendReview: (text: string, ids: string[]) => string }) => (
    <div data-testid="changes-tab-stub">
      Changes:{sessionId}
      <button type="button" onClick={() => onSendReview('REVIEW PROMPT', [])}>stub send review</button>
    </div>
  ),
}))

vi.mock('@/api/hooks', () => ({
  useSessionMcp: vi.fn(() => ({ data: undefined })),
  usePermissionRules: vi.fn(() => ({ data: [] })),
  useRevokePermissionRule: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useReconnectSessionMcp: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null })),
  useModels: () => ({ data: [] }),
  useAccounts: () => ({ data: [] }),
  useSessions: () => ({ data: [] }),
  useEngineOffers: vi.fn(() => []),
  useEngineCapabilities: vi.fn(() => undefined),
  useSessionsViewing: vi.fn(),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  useResolveSuspectedComplete: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useMarkSessionViewed: vi.fn(() => ({ mutate: vi.fn() })),
  useSetAccountPin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
  useSessionDiff: vi.fn(),
}))

import { useSessionDiff } from '@/api/hooks'

const WITH_FILES: SessionDiff = {
  base: 'abc', head: 'def', additions: 3, deletions: 1, truncated: false,
  files: [{ path: 'a.ts', oldPath: null, status: 'modified', additions: 3, deletions: 1, binary: false, tooLarge: false, hunks: [] }],
}
const NO_FILES: SessionDiff = { ...WITH_FILES, files: [], additions: 0, deletions: 0 }

const refetch = vi.fn()

function serve(d: SessionDiff) {
  vi.mocked(useSessionDiff).mockImplementation(() => queryResult(d, { refetch }))
}

function makeRuntime(status = 'idle'): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [], openBlockId: null, status: { state: status, count: 0 },
      title: null, model: null, usage: null,
      replaying: false, error: null, queued: [], rotations: [], latestSuggestions: [], subAgents: [],
    },
    mode: 'interactive', toolDetailMode: 'auto', toggleToolDetail: vi.fn(),
    connectionState: 'connected',
    title: 'Test', account: 'acct', quota: null, contextPct: null, subAgents: [],
    commands: [], backgroundWork: EMPTY_BACKGROUND_WORK_VIEW,
    hasOpenAction: false, armedQuestionId: null, chatArmed: false,
    onDecision: vi.fn(), onAnswer: vi.fn(), onChat: vi.fn(), onPrompt: vi.fn(), onPlainPrompt: vi.fn(() => 'sent'),
    onInterrupt: vi.fn(), onInterruptAndPrompt: vi.fn(), onSetMode: vi.fn(), onCancelQueued: vi.fn(),
  } as unknown as SessionRuntime
}

function renderBody(rt: SessionRuntime, path = '/sessions/s1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const ui = (r: SessionRuntime) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}><SessionBody rt={r} /></MemoryRouter>
    </QueryClientProvider>
  )
  const utils = render(ui(rt))
  return { ...utils, rerenderWith: (r: SessionRuntime) => utils.rerender(ui(r)) }
}

function x11(enabled: boolean) {
  vi.spyOn(global, 'fetch').mockResolvedValue(
    new Response(JSON.stringify(enabled ? { active_displays: 1 } : { enabled: false }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  refetch.mockReset()
  vi.mocked(useSessionDiff).mockReset()
})

describe('SessionBody — Changes tab', () => {
  it('is not listed when the summary has no files (wide pane: no strip at all)', async () => {
    setElementWidth(1200)
    onTestFinished(resetElementWidth)
    x11(false)
    serve(NO_FILES)
    renderBody(makeRuntime())
    await waitFor(() => expect(global.fetch).toHaveBeenCalled())
    expect(screen.queryByRole('tab', { name: /changes/i })).toBeNull()
    expect(screen.queryByRole('tablist')).toBeNull()
  })

  it('is listed when the summary has files, and opens the Changes pane', async () => {
    x11(false)
    serve(WITH_FILES)
    renderBody(makeRuntime())
    const tab = screen.getByRole('tab', { name: 'Changes' })
    await userEvent.click(tab)
    expect(await screen.findByTestId('changes-tab-stub')).toHaveTextContent('Changes:s1')
  })

  it('sits between Conversation and Browser; Details (a narrow pane) is last', async () => {
    x11(true)
    serve(WITH_FILES)
    renderBody(makeRuntime())
    await screen.findByRole('tab', { name: /browser/i })
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Conversation', 'Changes', 'Browser', 'Details'])
  })

  it('opens from ?view=changes without refetching the summary it just loaded', async () => {
    x11(false)
    serve(WITH_FILES)
    renderBody(makeRuntime(), '/sessions/s1?view=changes')
    expect(await screen.findByTestId('changes-tab-stub')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Changes' })).toHaveAttribute('aria-selected', 'true')
    expect(refetch).not.toHaveBeenCalled()
  })

  it('refetches the summary when the tab is opened', async () => {
    x11(false)
    serve(WITH_FILES)
    renderBody(makeRuntime())
    expect(refetch).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('tab', { name: 'Changes' }))
    await screen.findByTestId('changes-tab-stub')
    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('fetches the summary ONCE on a ?view=changes page load (real QueryClient)', async () => {
    x11(false)
    const actual = await vi.importActual<typeof import('@/api/hooks')>('@/api/hooks')
    vi.mocked(useSessionDiff).mockImplementation(actual.useSessionDiff)
    const { api } = await import('@/api/client')
    const get = vi.spyOn(api, 'get').mockResolvedValue(WITH_FILES)
    const summaryCalls = () => get.mock.calls.filter(([url]) => url === '/sessions/s1/diff?summary=1').length
    renderBody(makeRuntime(), '/sessions/s1?view=changes')
    expect(await screen.findByTestId('changes-tab-stub')).toBeInTheDocument()
    await new Promise((r) => setTimeout(r, 50))
    expect(summaryCalls()).toBe(1)
    // A real open (away and back) still refreshes it.
    await userEvent.click(screen.getByRole('tab', { name: 'Conversation' }))
    await userEvent.click(screen.getByRole('tab', { name: 'Changes' }))
    await waitFor(() => expect(summaryCalls()).toBe(2))
  })

  it('falls back to Conversation for ?view=changes with nothing changed', async () => {
    x11(false)
    serve(NO_FILES)
    renderBody(makeRuntime(), '/sessions/s1?view=changes')
    await waitFor(() => expect(global.fetch).toHaveBeenCalled())
    expect(screen.queryByTestId('changes-tab-stub')).toBeNull()
  })

  it('asks for the summary and passes busy so a turn end refetches it', () => {
    x11(false)
    serve(WITH_FILES)
    const { rerenderWith } = renderBody(makeRuntime('working'))
    expect(vi.mocked(useSessionDiff)).toHaveBeenLastCalledWith('s1', { summary: true, busy: true })
    rerenderWith(makeRuntime('idle'))
    expect(vi.mocked(useSessionDiff)).toHaveBeenLastCalledWith('s1', { summary: true, busy: false })
  })

  it('sends the review through the composer\'s prompt path (minus chat-arming), which queues while busy', async () => {
    x11(false)
    serve(WITH_FILES)
    const rt = makeRuntime('working')
    renderBody(rt, '/sessions/s1?view=changes')
    await userEvent.click(await screen.findByRole('button', { name: 'stub send review' }))
    // onPlainPrompt is onPrompt's send/queue path without the chat-armed branch; a
    // busy session queues it behind the live turn. Never the interrupting "Send now".
    expect(rt.onPlainPrompt).toHaveBeenCalledWith('REVIEW PROMPT', [])
    expect(rt.onPrompt).not.toHaveBeenCalled()
    expect(rt.onInterruptAndPrompt).not.toHaveBeenCalled()
  })
})
