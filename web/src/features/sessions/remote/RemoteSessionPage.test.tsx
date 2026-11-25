/**
 * The remote session page: attaches the shared runtime to `remote:<id>`, renders the
 * ordinary transcript and cards, routes the composer and the card actions to the REST
 * endpoints, hides ViberPit-only controls, and handles the contract's error codes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiRequestError } from '@/api/client'
import { transcriptReducer, initialTranscript, type TranscriptState } from '@/session/reducer'
import type { SessionEvent } from '@/session/events'
import type { RemoteSession } from '@/api/remoteSessionsHooks'
import { RemoteSessionPage } from './RemoteSessionPage'
import { REATTACH_WAIT_MS } from './useRemoteSessionRuntime'

type Listener = (e: unknown) => void

const h = vi.hoisted(() => ({
  runtimeIds: [] as string[],
  state: null as unknown,
  armed: null as string | null,
  reattach: vi.fn(),
  dismissError: vi.fn(),
  onChat: vi.fn(),
  listeners: new Map<string, Set<(e: unknown) => void>>(),
  get: vi.fn(),
  post: vi.fn(),
}))

vi.mock('@/session/useSessionRuntime', () => ({
  useSessionRuntime: (sessionId: string) => {
    h.runtimeIds.push(sessionId)
    return {
      sessionId,
      state: h.state,
      title: '',
      connectionState: 'connected',
      toolDetailMode: 'auto',
      thinkingCollapsed: true,
      viewModeEpoch: 0,
      armedQuestionId: h.armed,
      chatArmed: h.armed !== null,
      reattach: h.reattach,
      dismissError: h.dismissError,
      onChat: h.onChat,
    }
  },
}))

vi.mock('@/ws/client', async (orig) => ({
  ...(await orig<typeof import('@/ws/client')>()),
  useSessionSocket: () => ({
    on: (ev: string, cb: Listener) => {
      if (!h.listeners.has(ev)) h.listeners.set(ev, new Set())
      h.listeners.get(ev)!.add(cb)
    },
    off: (ev: string, cb: Listener) => { h.listeners.get(ev)?.delete(cb) },
  }),
}))

vi.mock('@/api/client', async (orig) => ({
  ...(await orig<typeof import('@/api/client')>()),
  api: { get: h.get, post: h.post, put: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const RECORD: RemoteSession = {
  id: 'session_r1', account: 'alice', title: 'Refactor the parser', status: 'running',
  connection: 'connected', machineId: 'feedc0ffee112233', origin: 'repl', cwd: '/w/parser',
  model: 'claude-opus', url: 'https://claude.ai/code/session_r1', updatedAt: null,
  needsYou: false, unread: false,
}

function stateFrom(events: SessionEvent[]): TranscriptState {
  // A live view: the attach replay has completed, so cards that arrive are live.
  const live = transcriptReducer(initialTranscript, { type: 'replayComplete', earliestTurn: 0, totalTurns: 0 })
  return events.reduce((s, e) => transcriptReducer(s, e), live)
}

const TEXT_TURN: SessionEvent[] = [
  { type: 'user', turnId: 'u1', text: 'Please refactor' } as SessionEvent,
  { type: 'turnStart', turnId: 't1' },
  { type: 'textDelta', turnId: 't1', text: 'Working on the parser now.' },
]

function emit(ev: string, payload: unknown) {
  for (const cb of h.listeners.get(ev) ?? []) cb(payload)
}

function refusal(status: number, code: string, message?: string) {
  return new ApiRequestError(status, { detail: { code, ...(message ? { message } : {}) } })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/sessions/remote/session_r1']}>
        <Routes>
          <Route path="/sessions/remote/:remoteId" element={<RemoteSessionPage remoteId="session_r1" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  h.runtimeIds = []
  h.listeners.clear()
  h.armed = null
  h.state = stateFrom(TEXT_TURN)
  h.get.mockImplementation((path: string) => Promise.resolve(
    path === '/remote-sessions' ? { enabled: true, sessions: [RECORD], accounts: [] }
      : path === '/sessions' ? [] : {},
  ))
  h.post.mockResolvedValue({ ok: true, sent: true, account: 'alice', kind: 'permission' })
})

describe('RemoteSessionPage — view', () => {
  it('attaches the shared runtime to the remote: id and renders the transcript', async () => {
    renderPage()
    expect(h.runtimeIds.every((id) => id === 'remote:session_r1')).toBe(true)
    expect(h.runtimeIds.length).toBeGreaterThan(0)
    expect(await screen.findByText('Working on the parser now.')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Refactor the parser' })).toBeInTheDocument()
  })

  it('shows where it runs and links out to the Claude app', async () => {
    renderPage()
    expect(await screen.findByText(
      'Running on machine feedc0ff — controlled via Remote Control',
    )).toBeInTheDocument()
    const link = await screen.findByRole('link', { name: /open in claude app/i })
    expect(link).toHaveAttribute('href', 'https://claude.ai/code/session_r1')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('drops an unsafe Claude app url', async () => {
    h.get.mockImplementation((path: string) => Promise.resolve(
      path === '/remote-sessions'
        ? { enabled: true, sessions: [{ ...RECORD, url: 'javascript:alert(1)' }], accounts: [] } : [],
    ))
    renderPage()
    await screen.findByRole('heading', { name: 'Refactor the parser' })
    expect(screen.queryByRole('link', { name: /open in claude app/i })).toBeNull()
  })

  it('hides the ViberPit-only controls', async () => {
    h.state = stateFrom([...TEXT_TURN, { type: 'status', state: 'in_progress', count: 0 }])
    renderPage()
    await screen.findByText('Working on the parser now.')
    // A busy turn still offers Queue (the CLI queues it) but no Stop: nothing to interrupt.
    expect(screen.getByRole('button', { name: /queue message/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /stop current turn/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /send now/i })).toBeNull()
    // No session-record controls: model / mode / effort, archive, rewind, account, rail.
    for (const name of [/model/i, /mode/i, /effort/i, /archive/i, /rewind/i, /account/i, /remote control/i]) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(screen.queryByRole('complementary', { name: /session details/i })).toBeNull()
  })

  it('offers no file attachment in the composer', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Working on the parser now.')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    expect(screen.queryByRole('menuitem', { name: /attach file/i })).toBeNull()
  })
})

describe('RemoteSessionPage — actions', () => {
  it('sends a composer prompt to the prompt endpoint', async () => {
    const user = userEvent.setup()
    renderPage()
    const box = await screen.findByRole('textbox', { name: /message/i })
    await user.type(box, 'Add a test too')
    await user.click(screen.getByRole('button', { name: /send message/i }))
    expect(h.post).toHaveBeenCalledWith('/remote-sessions/session_r1/prompt', { text: 'Add a test too' })
  })

  it('answers a permission card through the answer endpoint', async () => {
    const user = userEvent.setup()
    h.state = stateFrom([...TEXT_TURN, {
      type: 'permissionRequest', permId: 'req-9', turnId: 't1', tool: 'Bash', toolKind: 'bash',
      input: { command: 'rm -rf build' }, plan: false, alwaysAllowed: false, sessionGrantEffective: false,
    }])
    renderPage()
    const panel = await screen.findByTestId('pending-permission-panel')
    // No standing grants on a remote session: Allow once and Deny only.
    expect(within(panel).queryByRole('button', { name: /always allow|for this session/i })).toBeNull()
    await user.click(within(panel).getByRole('button', { name: /^allow bash/i }))
    expect(h.post).toHaveBeenCalledWith('/remote-sessions/session_r1/answer', { askId: 'req-9', decision: 'allow' })
    await user.click(within(panel).getByRole('button', { name: /^deny/i }))
    expect(h.post).toHaveBeenCalledWith('/remote-sessions/session_r1/answer', { askId: 'req-9', decision: 'deny' })
  })

  it('answers a question card through the answer endpoint', async () => {
    const user = userEvent.setup()
    h.state = stateFrom([...TEXT_TURN, {
      type: 'question', questionId: 'req-4:0', turnId: 't1', header: 'Pick', question: 'Which parser?',
      options: [{ label: 'Pratt' }, { label: 'PEG' }], multiSelect: false,
    }])
    renderPage()
    const panel = await screen.findByTestId('pending-question-panel')
    await user.click(within(panel).getByRole('radio', { name: 'PEG' }))
    expect(h.post).toHaveBeenCalledWith(
      '/remote-sessions/session_r1/answer', { askId: 'req-4:0', selected: ['PEG'], mode: 'option' },
    )
  })

  it('sends the composer text as the answer to a question armed for chat', async () => {
    const user = userEvent.setup()
    h.armed = 'req-4:0'
    renderPage()
    const box = await screen.findByRole('textbox', { name: /message/i })
    await user.type(box, 'Neither — hand-roll it')
    await user.click(screen.getByRole('button', { name: /send reply/i }))
    expect(h.post).toHaveBeenCalledWith(
      '/remote-sessions/session_r1/answer',
      { askId: 'req-4:0', selected: ['Neither — hand-roll it'], mode: 'chat' },
    )
    await waitFor(() => expect(h.onChat).toHaveBeenCalledWith('req-4:0'))
  })
})

describe('RemoteSessionPage — errors', () => {
  it('re-attaches on remote_stream_reset', async () => {
    h.state = { ...stateFrom(TEXT_TURN), error: { code: 'remote_stream_reset', message: 'reset' } }
    renderPage()
    await waitFor(() => expect(h.reattach).toHaveBeenCalled())
    expect(h.dismissError).toHaveBeenCalled()
  })

  it('shows a banner and drops the composer when the session is gone', async () => {
    h.state = { ...stateFrom(TEXT_TURN), error: { code: 'remote_session_gone', message: 'gone' } }
    renderPage()
    expect(await screen.findByText('This session is gone')).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /message/i })).toBeNull()
  })

  it('treats a NOT_FOUND write as gone', async () => {
    const user = userEvent.setup()
    h.post.mockRejectedValueOnce(refusal(404, 'REMOTE_SESSION_NOT_FOUND', 'No such session'))
    renderPage()
    await user.type(await screen.findByRole('textbox', { name: /message/i }), 'hello')
    await user.click(screen.getByRole('button', { name: /send message/i }))
    expect(await screen.findByText('This session is gone')).toBeInTheDocument()
  })

  it('shows the person-only refusal inline and returns the draft', async () => {
    const user = userEvent.setup()
    h.post.mockRejectedValueOnce(refusal(403, 'SESSION_CONTROL_HUMAN_ONLY', 'Only a person can do that.'))
    renderPage()
    const box = await screen.findByRole('textbox', { name: /message/i })
    await user.type(box, 'do the thing')
    await user.click(screen.getByRole('button', { name: /send message/i }))
    expect(await screen.findByText('Only a person can do that.')).toBeInTheDocument()
    await waitFor(() => expect(box).toHaveValue('do the thing'))
  })

  it('re-attaches and retries once on REMOTE_SESSION_NOT_WATCHED', async () => {
    const user = userEvent.setup()
    h.state = stateFrom([...TEXT_TURN, {
      type: 'permissionRequest', permId: 'req-9', turnId: 't1', tool: 'Bash', toolKind: 'bash',
      input: { command: 'ls' }, plan: false, alwaysAllowed: false, sessionGrantEffective: false,
    }])
    h.post.mockRejectedValueOnce(refusal(409, 'REMOTE_SESSION_NOT_WATCHED'))
    renderPage()
    const panel = await screen.findByTestId('pending-permission-panel')
    await user.click(within(panel).getByRole('button', { name: /^allow bash/i }))
    await waitFor(() => expect(h.reattach).toHaveBeenCalledTimes(1))
    expect(h.post).toHaveBeenCalledTimes(1)
    // The retry waits for the re-attach's replay to finish.
    act(() => emit('sessionEvent', { type: 'replayComplete', sessionId: 'remote:session_r1', earliestTurn: 0, totalTurns: 0 }))
    await waitFor(() => expect(h.post).toHaveBeenCalledTimes(2))
    expect(h.post).toHaveBeenLastCalledWith('/remote-sessions/session_r1/answer', { askId: 'req-9', decision: 'allow' })
  })

  it('gives up after one retry and says so', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
      h.post
        .mockRejectedValueOnce(refusal(409, 'REMOTE_SESSION_NOT_WATCHED'))
        .mockRejectedValueOnce(refusal(409, 'REMOTE_SESSION_NOT_WATCHED', 'Open the session to answer.'))
      renderPage()
      await user.type(await screen.findByRole('textbox', { name: /message/i }), 'hi')
      await user.click(screen.getByRole('button', { name: /send message/i }))
      await act(async () => { vi.advanceTimersByTime(REATTACH_WAIT_MS) })
      expect(await screen.findByText('Open the session to answer.')).toBeInTheDocument()
      expect(h.post).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})
