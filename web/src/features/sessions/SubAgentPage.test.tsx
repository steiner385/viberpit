import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { resetDocumentTitle } from '@/shell/documentTitle'

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

import { SubAgentReaderPage } from './SubAgentPage'

function view(path = '/sessions/s1/agents/toolu_A') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SubAgentReaderPage sessionId="s1" agentId="toolu_A" />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mutate.mockReset(); onDecision.mockReset(); onAnswer.mockReset()
  stopIsSuccess = false
  resetDocumentTitle()
  thread = {
    turns: [{ id: 's1', role: 'assistant', blocks: [{ kind: 'text', id: 's1:t0', text: 'SUBWORK' }] }],
    openBlockId: null,
    task: { taskId: 'task1', description: 'map handling', agentType: 'Explore', backgrounded: false, depth: 1, status: 'running', startedAt: Date.now() - 65_000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 3 },
  }
  agents = [{ toolId: 'toolu_A', type: 'Explore', description: 'map handling', status: 'running', startedAt: Date.now() - 65_000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 3, taskId: 'task1' }]
})

describe('SubAgentReaderPage', () => {
  it('is a Reader page: one h1 = type › description, document.title, variant, Back link into the scope', async () => {
    view()
    expect(screen.getByRole('heading', { level: 1, name: 'Explore › map handling' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    await waitFor(() => expect(document.title).toBe('Explore › map handling — ViberPit'))
    expect(document.querySelector('[data-page-variant="reader"]')).not.toBeNull()
    expect(screen.getByRole('link', { name: 'Back to session' })).toHaveAttribute('href', '/sessions/s1')
  })

  it('the Back link and breadcrumb stay inside the background scope', () => {
    view('/sessions/background/s1/agents/toolu_A')
    expect(screen.getByRole('link', { name: 'Back to session' })).toHaveAttribute('href', '/sessions/background/s1')
    expect(screen.getByRole('link', { name: 'Parent Session' })).toHaveAttribute('href', '/sessions/background/s1')
  })

  it('renders the sub-agent thread, not the main turn, with a breadcrumb and no composer', () => {
    view()
    expect(screen.getByText(/SUBWORK/)).toBeInTheDocument()
    expect(screen.queryByText(/MAIN_ONLY/)).toBeNull()
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' })).toHaveTextContent('Parent Session')
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('shows the live status line, usage and activity', () => {
    view()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText(/19,591 tokens/)).toBeInTheDocument()
    expect(screen.getByText(/3 tools/)).toBeInTheDocument()
    expect(screen.getByText(/last: Bash/)).toBeInTheDocument()
    expect(screen.getByText('Running Bash')).toBeInTheDocument()
  })

  it('Stop is the header action and calls the mutation with the session and tool-use id', () => {
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
    agents = [{ ...(agents[0] as object), status: 'stopped', summary: 'halted by user', taskId: 'task1' }]
    view()
    expect(screen.getByText('stopped')).toBeInTheDocument()
    expect(screen.getByText('halted by user')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Stop sub-agent/ })).toBeNull()
  })

  it('a permission card in the sub-thread is answerable in place', () => {
    thread = {
      ...thread,
      turns: [
        ...(thread.turns as unknown[]),
        { id: 'p1', role: 'assistant', blocks: [{ kind: 'permission', id: 'p1', permId: 'p1', tool: 'Write', toolKind: 'write', input: { file_path: '/tmp/x' }, plan: false }] },
      ],
    }
    view()
    fireEvent.click(screen.getByRole('button', { name: /^Allow / }))
    expect(onDecision).toHaveBeenCalledWith('p1', 'allow')
  })

  it('shows the starting placeholder for a running agent with no turns yet', () => {
    thread = { ...thread, turns: [] }
    view()
    expect(screen.getByText(/Sub-agent starting/)).toBeInTheDocument()
  })
})
