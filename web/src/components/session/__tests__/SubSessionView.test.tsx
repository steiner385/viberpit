import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'

const mutate = vi.fn()
let stopIsSuccess = false
vi.mock('@/api/hooks', () => ({
  useStopSubAgent: () => ({ mutate, isPending: false, isSuccess: stopIsSuccess }),
}))

const onDecision = vi.fn()
const onAnswer = vi.fn()
let thread: Record<string, unknown>
let agents: unknown[]
vi.mock('@/session/useSessionRuntime', () => ({
  useSessionRuntime: () => ({
    sessionId: 's1', title: 'Parent Session', mode: 'interactive',
    toolDetailMode: 'compact', thinkingCollapsed: false,
    subAgents: agents, armedQuestionId: null,
    onDecision, onAnswer, onChat: vi.fn(),
    state: {
      turns: [{ id: 'm1', role: 'assistant', blocks: [{ kind: 'text', id: 'm1:t0', text: 'MAIN_ONLY' }] }],
      subAgents: { toolu_A: thread },
    },
  }),
}))

import { SubSessionView } from '../SubSessionView'

function view() {
  return render(<MemoryRouter><SubSessionView sessionId="s1" agentId="toolu_A" onBack={() => {}} /></MemoryRouter>)
}

beforeEach(() => {
  mutate.mockReset(); onDecision.mockReset(); onAnswer.mockReset()
  stopIsSuccess = false
  thread = {
    turns: [{ id: 's1', role: 'assistant', blocks: [{ kind: 'text', id: 's1:t0', text: 'SUBWORK' }] }],
    openBlockId: null,
    task: { taskId: 'task1', description: 'map handling', agentType: 'Explore', backgrounded: false, depth: 1, status: 'running', startedAt: Date.now() - 65_000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 3 },
  }
  agents = [{ toolId: 'toolu_A', type: 'Explore', description: 'map handling', status: 'running', startedAt: Date.now() - 65_000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 3, taskId: 'task1' }]
})

describe('SubSessionView', () => {
  it('renders the sub-agent thread, not the main turn, with a breadcrumb', () => {
    view()
    expect(screen.getByText(/SUBWORK/)).toBeInTheDocument()
    expect(screen.queryByText(/MAIN_ONLY/)).toBeNull()
    expect(screen.getByText(/Parent Session/)).toBeInTheDocument()
    expect(screen.queryByText(/Read-only/)).toBeNull()
  })

  it('shows the live header: type › description, status, usage line and activity', () => {
    view()
    expect(screen.getByRole('heading', { name: /Explore › map handling/ })).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText(/19,591 tokens/)).toBeInTheDocument()
    expect(screen.getByText(/3 tools/)).toBeInTheDocument()
    expect(screen.getByText(/last: Bash/)).toBeInTheDocument()
    expect(screen.getByText('Running Bash')).toBeInTheDocument()
  })

  it('Stop calls the mutation with the session and tool-use id', () => {
    view()
    fireEvent.click(screen.getByRole('button', { name: /Stop sub-agent/ }))
    expect(mutate).toHaveBeenCalledWith({ id: 's1', toolUseId: 'toolu_A' })
  })

  it('hides Stop for a running agent with no live task state (replayed/detached)', () => {
    agents = [{ ...(agents[0] as object), taskId: undefined }]
    view()
    expect(screen.queryByRole('button', { name: /Stop sub-agent/ })).toBeNull()
  })

  it('disables Stop once the mutation succeeds (until subAgentEnded unmounts it)', () => {
    stopIsSuccess = true
    view()
    expect(screen.getByRole('button', { name: /Stop sub-agent/ })).toBeDisabled()
  })

  it('a finished sub-agent shows its status and summary and no Stop button', () => {
    thread = { ...thread, task: { ...(thread.task as object), status: 'stopped', endedAt: Date.now(), summary: 'halted by user' } }
    agents = [{ ...(agents[0] as object), status: 'stopped', summary: 'halted by user' }]
    view()
    expect(screen.getByText('stopped')).toBeInTheDocument()
    expect(screen.getByText('halted by user')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Stop sub-agent/ })).toBeNull()
  })

  it('a permission card in the sub-thread is answerable in place', () => {
    thread = { ...thread, turns: [{ id: 's1', role: 'assistant', blocks: [{ kind: 'permission', id: 'p1', permId: 'p1', tool: 'Write', toolKind: 'write', input: { file_path: '/tmp/x' }, plan: false }] }] }
    view()
    fireEvent.click(screen.getByRole('button', { name: /^Allow / }))
    expect(onDecision).toHaveBeenCalledWith('p1', 'allow')
  })
})
