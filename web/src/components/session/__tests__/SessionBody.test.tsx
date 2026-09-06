import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { SessionBody } from '../SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { Block } from '@/session/reducer'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import type { SessionEvent } from '@/session/events'
import * as hooks from '@/api/hooks'
import { queryResult } from '@/test/reactQuery'
import type { AgentCapabilities, EngineOffer, Session } from '@/api/types'
import { session as makeSession } from '@/test/factories'

// SessionBody now calls useModels/useAccounts/useSessions (account+model switchers).
// Stub them so this layout test needs no QueryClientProvider or network.
// `vi.hoisted` because the mock factory below is hoisted above ordinary consts.
const { useEngineOffers } = vi.hoisted(() => ({
  useEngineOffers: vi.fn((): { engine: string; capabilities?: unknown }[] => []),
}))

vi.mock('@/api/hooks', () => ({
  useModels: vi.fn(() => ({ data: [] })),
  useAccounts: vi.fn(() => ({ data: [] })),
  useSessions: vi.fn(() => ({ data: [] })),
  // Engine capabilities. `useEngineCapabilities` DELEGATES to the mocked
  // `useEngineOffers` rather than being a second stub, so a test that seeds
  // offers still drives the gate it is asserting on.
  useEngineOffers,
  useEngineCapabilities: (engine?: string) =>
    useEngineOffers().find(
      (o: { engine: string }) => o.engine === (engine ?? 'claude_code'),
    )?.capabilities,
  useSessionsViewing: vi.fn(),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  // Unpin account button (SessionBody → SessionMeta).
  useSetAccountPin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

// Regression: the desktop master-detail shell renders SessionBody but previously
// did NOT render PendingQuestionPanel, so a live AskUserQuestion (filtered out of
// the inline transcript by Turn) appeared nowhere on desktop and was unanswerable.
// PendingQuestionPanel now lives inside SessionBody, so both layouts surface it.

const question = (id: string, live: boolean): Block => ({
  kind: 'question', id, questionId: id, header: id, question: `${id}?`,
  options: [{ label: 'Yes' }], multiSelect: false, live,
})

function makeRuntime(blocks: Block[]): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [{ id: 't1', role: 'assistant', blocks }],
      openBlockId: null,
      status: { state: 'idle', count: 0 },
      title: null, model: null, usage: null,
      replaying: false, error: null, queued: [], rotations: [], latestSuggestions: [],
    },
    mode: 'interactive',
    toolDetailMode: 'auto',
    thinkingCollapsed: true,
    connectionState: 'connected',
    title: 'Test', account: 'acct', quota: null, contextPct: null, subAgents: [],
    hasOpenAction: false, hasOpenPermission: false,
    armedQuestionId: null, chatArmed: false,
    onDecision: vi.fn(), onAnswer: vi.fn(), onChat: vi.fn(), onPrompt: vi.fn(),
    onInterrupt: vi.fn(), onSetMode: vi.fn(), onCancelQueued: vi.fn(),
  } as unknown as SessionRuntime
}

function makeEmptyRuntime(): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [],
      openBlockId: null,
      status: { state: 'idle', count: 0 },
      title: null, model: null, usage: null,
      replaying: false, error: null, queued: [], rotations: [], latestSuggestions: [],
    },
    mode: 'interactive',
    toolDetailMode: 'auto',
    thinkingCollapsed: true,
    connectionState: 'connected',
    title: 'Test', account: 'acct', quota: null, contextPct: null, subAgents: [],
    hasOpenAction: false, hasOpenPermission: false,
    armedQuestionId: null, chatArmed: false,
    onDecision: vi.fn(), onAnswer: vi.fn(), onChat: vi.fn(), onPrompt: vi.fn(),
    onInterrupt: vi.fn(), onSetMode: vi.fn(), onCancelQueued: vi.fn(),
  } as unknown as SessionRuntime
}

describe('SessionBody', () => {
  beforeEach(() => {
    // WorkingIndicator (rendered for busy states) reads prefers-reduced-motion.
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
      })),
    })
    vi.mocked(hooks.useModels).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useEngineOffers).mockReturnValue([])
  })

  it('renders the pinned answer panel for a LIVE open question (desktop + mobile)', () => {
    render(<SessionBody rt={makeRuntime([question('a', true)])} />)
    expect(screen.getByTestId('pending-question-panel')).toBeInTheDocument()
  })

  it('does not render the panel when the only question is historical (replayed)', () => {
    render(<SessionBody rt={makeRuntime([question('a', false)])} />)
    expect(screen.queryByTestId('pending-question-panel')).toBeNull()
  })

  it('renders "Queued — position 3" placeholder for a queued session with queuePosition 3', () => {
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [{ id: 's1', status: 'queued', queuePosition: 3 }],
    } as ReturnType<typeof hooks.useSessions>)
    render(<SessionBody rt={makeEmptyRuntime()} />)
    expect(screen.getByText(/Queued — position 3/i)).toBeInTheDocument()
  })

  it('pins the Your-turn panel with one-tap suggestion replies when the status is done', () => {
    const rt = makeRuntime([])
    rt.state.status = { state: 'done', count: 0 }
    rt.state.latestSuggestions = ['Run the tests']
    render(<SessionBody rt={rt} />)
    expect(screen.getByTestId('your-turn-panel')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Run the tests' }))
    expect(rt.onPrompt).toHaveBeenCalledWith('Run the tests', [])
  })

  it('falls back to the session record suggestions when the runtime has none (detached session)', () => {
    // A detached done session never received a live `suggestions` WS event, so
    // rt.state.latestSuggestions is empty — but the record cache (what the
    // session card's chips use) still has them.
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [{ id: 's1', status: 'done', latestSuggestions: ['Proceed with Phase B'] }],
    } as ReturnType<typeof hooks.useSessions>)
    const rt = makeRuntime([])
    rt.state.status = { state: 'done', count: 0 }
    rt.state.latestSuggestions = []
    render(<SessionBody rt={rt} />)
    expect(screen.getByRole('button', { name: 'Proceed with Phase B' })).toBeInTheDocument()
  })

  it('does not pin the Your-turn panel while a turn is running', () => {
    const rt = makeRuntime([])
    rt.state.status = { state: 'in_progress', count: 1 }
    render(<SessionBody rt={rt} />)
    expect(screen.queryByTestId('your-turn-panel')).toBeNull()
  })
})

// ── engine capabilities (Task 5.3) ──────────────────────────────────────────
//
// The plan's fixed contract: engine CAPABILITIES, not `engine === 'codex'`
// comparisons, decide which controls render. SessionBody is where the session's
// engine meets `/api/config`'s per-engine capability report.

describe('SessionBody — capability-gated surfaces', () => {
  // The two real capability sets: server/sessions/claude_driver.py
  // `ClaudeAgentDriver.capabilities` and server/codex/capabilities.py
  // `CODEX_CAPABILITIES`, as `/api/config` serialises them.
  const CLAUDE_CAPS: AgentCapabilities = {
    models: true, reasoningEffort: false, steer: false, interrupt: true, approvals: true,
    questions: true, fork: false, adopt: true, selfModelSwitch: true, subagents: true,
    attachments: true, accountRotation: true, accountSwitchNewThread: false,
  }
  const CODEX_CAPS: AgentCapabilities = {
    models: true, reasoningEffort: true, steer: true, interrupt: true, approvals: true,
    questions: false, fork: false, adopt: false, selfModelSwitch: false, subagents: false,
    attachments: true, accountRotation: false, accountSwitchNewThread: true,
  }

  const offer = (engine: EngineOffer['engine'], capabilities: AgentCapabilities): EngineOffer => ({
    engine, available: true, hosts: ['dobby'], unavailableReasons: {}, capabilities,
  })

  function setConfig() {
    vi.mocked(hooks.useEngineOffers).mockReturnValue([
      offer('claude_code', CLAUDE_CAPS),
      offer('codex', CODEX_CAPS),
    ])
  }

  function setSession(over: Partial<Session>) {
    vi.mocked(hooks.useSessions).mockReturnValue(
      queryResult([makeSession({ id: 's1', ...over })]),
    )
  }

  beforeEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
      })),
    })
    vi.mocked(hooks.useModels).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([]))
    setConfig()
  })

  it('hides the question panel for an engine without the questions capability', () => {
    setSession({ engine: 'codex' })
    render(<SessionBody rt={makeRuntime([question('a', true)])} />)
    expect(screen.queryByTestId('pending-question-panel')).toBeNull()
  })

  it('keeps the question panel for an engine that has the capability', () => {
    setSession({ engine: 'claude_code' })
    render(<SessionBody rt={makeRuntime([question('a', true)])} />)
    expect(screen.getByTestId('pending-question-panel')).toBeInTheDocument()
  })

  it('hides the parked model-approval panel for an engine without self model switching', () => {
    setSession({ engine: 'codex', pendingModel: 'gpt-5.1-codex' })
    render(<SessionBody rt={makeRuntime([])} />)
    expect(screen.queryByTestId('pending-model-approval-panel')).toBeNull()
  })

  it('keeps the parked model-approval panel for Claude', () => {
    setSession({ engine: 'claude_code', pendingModel: 'fable' })
    render(<SessionBody rt={makeRuntime([])} />)
    expect(screen.getByTestId('pending-model-approval-panel')).toBeInTheDocument()
  })

  it('renders the session identity (agent, runner, reasoning effort) in the meta strip', () => {
    setSession({ engine: 'codex', host: 'rocky', reasoningEffort: 'high' })
    render(<SessionBody rt={makeRuntime([])} />)
    expect(screen.getByLabelText('Agent')).toHaveTextContent('Codex')
    expect(screen.getByLabelText('Runner host')).toHaveTextContent('rocky')
    expect(screen.getByLabelText('Reasoning effort')).toHaveTextContent('high')
  })

  it('renders a Codex approval through the existing pending-permission UX', () => {
    // A Codex approval request is normalised to the SAME permission event as a
    // Claude one — tool "Bash" with {command, cwd}
    // (server/codex/driver.py `_approval_shape`) — so it must render in the
    // pinned panel with no Codex-specific card. The decisions offered are the
    // ccswitch triple allow / deny / always, which the Codex driver relays as
    // the app-server's own `accept` / `decline` / `acceptForSession`
    // (server/codex/schema.py COMMAND_APPROVAL_DECISIONS). `cancel` is in that
    // wire vocabulary but is never offered: ccswitch only ever relays a human
    // decision, and the driver has no path that emits it.
    setSession({ engine: 'codex' })
    const rt = makeRuntime([{
      kind: 'permission', id: 'p1', permId: 'p1', tool: 'Bash', toolKind: 'bash',
      input: { command: 'pytest -q', cwd: '/srv/repo' }, plan: false,
    }])
    rt.mode = 'auto'
    render(<SessionBody rt={rt} />)
    expect(screen.getByTestId('pending-permission-panel')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Allow Bash: pytest -q$/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Deny Bash: pytest -q$/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^Always allow Bash: pytest -q$/ }))
    expect(rt.onDecision).toHaveBeenCalledWith('p1', 'always')
  })

  it('a reconnect replay does not resurrect an already-resolved approval', () => {
    // The reattach path replays the open turn (`replayReset` + the ring), which
    // re-emits the permissionRequest of a request the user already answered.
    // Reduced through the real reducer, the replayed pair must leave NOTHING
    // pinned — a resurrected card is a prompt whose decision the daemon has
    // already consumed, so pressing it does nothing.
    const perm: SessionEvent = {
      type: 'permissionRequest', permId: 'p1', turnId: 't1', tool: 'Bash',
      toolKind: 'bash', input: { command: 'rm -rf build', cwd: '/srv/repo' }, plan: false,
    }
    const resolved: SessionEvent = {
      type: 'permissionResolved', permId: 'p1', turnId: 't1', allowed: true,
    }
    const events: SessionEvent[] = [
      { type: 'turnStart', turnId: 't1' },
      perm,
      resolved,
      // …reconnect: the server replays the turn from its ring.
      { type: 'replayReset' },
      { type: 'turnStart', turnId: 't1' },
      perm,
      resolved,
      { type: 'replayComplete' },
    ]
    const state = events.reduce(transcriptReducer, initialTranscript)
    expect(state.turns.flatMap((t) => t.blocks).filter((b) => b.kind === 'permission')).toEqual([])

    const rt = makeRuntime([])
    rt.state.turns = state.turns
    render(<SessionBody rt={rt} />)
    expect(screen.queryByTestId('pending-permission-panel')).toBeNull()
  })

  it('leaves every surface in place while the capability report is still loading', () => {
    // An unanswered config must never REMOVE a control that works today.
    vi.mocked(hooks.useEngineOffers).mockReturnValue([])
    setSession({ engine: 'claude_code', pendingModel: 'fable' })
    render(<SessionBody rt={makeRuntime([question('a', true)])} />)
    expect(screen.getByTestId('pending-question-panel')).toBeInTheDocument()
    expect(screen.getByTestId('pending-model-approval-panel')).toBeInTheDocument()
  })
})
