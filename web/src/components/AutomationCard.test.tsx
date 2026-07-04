// web/src/components/AutomationCard.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AutomationCard } from './AutomationCard'
import * as hooks from '@/api/hooks'
import type { Automation } from '@/api/automations'
import { toast } from 'sonner'
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}))

const PR_DOCTOR: Automation = {
  id: 'pr-doctor', name: 'PR Doctor', description: 'watches CI',
  enabled: false, mode: 'propose', modes: ['off', 'auto', 'propose'],
  status: { text: 'idle', level: 'idle', counts: null },
  configSchema: [], config: {}, lastActiveAt: null, activeSessions: 0,
}

const WITH_SCHEMA: Automation = {
  ...PR_DOCTOR,
  configSchema: [
    { key: 'draft_only', label: 'Draft PRs only', type: 'bool', default: false, help: 'Only watch draft PRs', choices: null },
    { key: 'max_retries', label: 'Max retries', type: 'int', default: 3, help: '', choices: null },
    { key: 'prefix', label: 'Prefix', type: 'string', default: 'fix:', help: '', choices: null },
    { key: 'strategy', label: 'Strategy', type: 'enum', default: 'fast', help: '', choices: ['fast', 'thorough'] },
    { key: 'watched_repos', label: 'Watched repos', type: 'repo-multiselect', default: [], help: 'Repos to watch', choices: null },
  ],
  config: {
    draft_only: true,
    max_retries: 5,
    prefix: 'bot:',
    strategy: 'thorough',
    watched_repos: ['owner/repo-a'],
  },
}

const MOCK_REPOS = [
  { nameWithOwner: 'owner/repo-a', name: 'repo-a', owner: 'owner', isPrivate: false, url: '', defaultBranch: 'main', pushedAt: null, cloned: true },
  { nameWithOwner: 'owner/repo-b', name: 'repo-b', owner: 'owner', isPrivate: false, url: '', defaultBranch: 'main', pushedAt: null, cloned: false },
]

function mockBasicHooks(mutate = vi.fn()) {
  vi.spyOn(hooks, 'usePatchAutomation').mockReturnValue(
    { mutate } as unknown as ReturnType<typeof hooks.usePatchAutomation>)
  vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue(
    { data: [] } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
  vi.spyOn(hooks, 'useAutomationSessions').mockReturnValue(
    { data: [] } as unknown as ReturnType<typeof hooks.useAutomationSessions>)
  vi.spyOn(hooks, 'useGithubRepos').mockReturnValue(
    { data: MOCK_REPOS, isLoading: false, isError: false } as unknown as ReturnType<typeof hooks.useGithubRepos>)
  vi.spyOn(hooks, 'useAutomationActionable').mockReturnValue(
    { data: [] } as unknown as ReturnType<typeof hooks.useAutomationActionable>)
  vi.spyOn(hooks, 'useAutomationTrigger').mockReturnValue(
    { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useAutomationTrigger>)
  vi.spyOn(hooks, 'useConsolidation').mockReturnValue(
    { data: undefined, isLoading: false } as unknown as ReturnType<typeof hooks.useConsolidation>)
  vi.spyOn(hooks, 'useConsolidate').mockReturnValue(
    { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useConsolidate>)
}

describe('AutomationCard', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders name, status, and a toggle', () => {
    mockBasicHooks()
    render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
    expect(screen.getByText('PR Doctor')).toBeInTheDocument()
    expect(screen.getByText('idle')).toBeInTheDocument()
    expect(screen.getByRole('switch')).toBeInTheDocument()
  })

  it('toggling enabled calls patch', () => {
    const mutate = vi.fn()
    mockBasicHooks(mutate)
    render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('switch'))
    expect(mutate).toHaveBeenCalledWith({ id: 'pr-doctor', body: { enabled: true } })
  })

  it('enabling a 2-mode automation with no mode also sets the active mode', () => {
    // CI/CD Doctor (off/propose) hides the mode <select>, so the toggle must
    // carry the mode — else it enables with mode NULL and stays stuck 'off'.
    const mutate = vi.fn()
    mockBasicHooks(mutate)
    const cicd: Automation = {
      ...PR_DOCTOR, id: 'cicd-doctor', name: 'CI/CD Doctor',
      enabled: false, mode: null, modes: ['off', 'propose'],
    }
    render(<MemoryRouter><AutomationCard automation={cicd} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('switch'))
    expect(mutate).toHaveBeenCalledWith({ id: 'cicd-doctor', body: { enabled: true, mode: 'propose' } })
  })

  it('disabling never sends a mode', () => {
    const mutate = vi.fn()
    mockBasicHooks(mutate)
    const cicd: Automation = {
      ...PR_DOCTOR, id: 'cicd-doctor', name: 'CI/CD Doctor',
      enabled: true, mode: 'propose', modes: ['off', 'propose'],
    }
    render(<MemoryRouter><AutomationCard automation={cicd} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('switch'))
    expect(mutate).toHaveBeenCalledWith({ id: 'cicd-doctor', body: { enabled: false } })
  })

  // --- Active-session header badge ---

  describe('Active-session badge', () => {
    it('shows the active count in the collapsed header when > 0', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={{ ...PR_DOCTOR, activeSessions: 3 }} /></MemoryRouter>)
      // Visible without expanding.
      expect(screen.getByText('3 active')).toBeInTheDocument()
    })

    it('omits the badge when there are no active sessions', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      expect(screen.queryByText(/\bactive\b/)).not.toBeInTheDocument()
    })
  })

  // --- Activity log ---

  describe('Activity log', () => {
    it('opens the linked session via onOpenSession when a log line is clicked', () => {
      const onOpenSession = vi.fn()
      mockBasicHooks()
      vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue({
        data: [{ ts: '2026-07-02T10:11:12Z', level: 'info', kind: 'spawned',
                 summary: 'spawned fixer', target: null, sessionId: 'sess-9' }],
      } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} onOpenSession={onOpenSession} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      fireEvent.click(screen.getByRole('button', { name: 'spawned fixer' }))
      expect(onOpenSession).toHaveBeenCalledWith('sess-9')
    })
  })

  // --- Spawned sessions section ---

  describe('Spawned sessions', () => {
    it('shows empty state when no sessions', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      // Expand the card
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      expect(screen.getByText('No sessions spawned.')).toBeInTheDocument()
    })

    it('opens a spawned session via onOpenSession (not the router) when clicked', () => {
      const onOpenSession = vi.fn()
      vi.spyOn(hooks, 'usePatchAutomation').mockReturnValue(
        { mutate: vi.fn() } as unknown as ReturnType<typeof hooks.usePatchAutomation>)
      vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue(
        { data: [] } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
      vi.spyOn(hooks, 'useAutomationSessions').mockReturnValue({
        data: [{ id: 'sess-1', name: 'Fix PR #42', status: 'in_progress', originRef: 'pr/42' }],
      } as unknown as ReturnType<typeof hooks.useAutomationSessions>)
      vi.spyOn(hooks, 'useGithubRepos').mockReturnValue(
        { data: [], isLoading: false } as unknown as ReturnType<typeof hooks.useGithubRepos>)
      vi.spyOn(hooks, 'useAutomationActionable').mockReturnValue(
        { data: [] } as unknown as ReturnType<typeof hooks.useAutomationActionable>)
      vi.spyOn(hooks, 'useAutomationTrigger').mockReturnValue(
        { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useAutomationTrigger>)
      vi.spyOn(hooks, 'useConsolidation').mockReturnValue(
        { data: undefined, isLoading: false } as unknown as ReturnType<typeof hooks.useConsolidation>)
      vi.spyOn(hooks, 'useConsolidate').mockReturnValue(
        { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useConsolidate>)

      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} onOpenSession={onOpenSession} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))

      const openBtn = screen.getByRole('button', { name: 'Fix PR #42' })
      expect(openBtn).toBeInTheDocument()
      expect(screen.getByText('· in_progress')).toBeInTheDocument()
      fireEvent.click(openBtn)
      expect(onOpenSession).toHaveBeenCalledWith('sess-1')
    })

    it('reviews an archived spawned session via onReviewSession (not onOpenSession)', () => {
      const onOpenSession = vi.fn()
      const onReviewSession = vi.fn()
      mockBasicHooks()
      vi.spyOn(hooks, 'useAutomationSessions').mockReturnValue({
        data: [{ id: 'sess-2', name: 'Implement issue 16', status: 'archived', originRef: 'owner/repo#16' }],
      } as unknown as ReturnType<typeof hooks.useAutomationSessions>)

      render(<MemoryRouter>
        <AutomationCard automation={PR_DOCTOR} onOpenSession={onOpenSession} onReviewSession={onReviewSession} />
      </MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))

      fireEvent.click(screen.getByRole('button', { name: 'Implement issue 16' }))
      // Archived sessions have no live driver — they must open the read-only review
      // route, not the live-attach route (which would 404 with SESSION_NOT_FOUND).
      expect(onReviewSession).toHaveBeenCalledWith('sess-2')
      expect(onOpenSession).not.toHaveBeenCalled()
    })

    it('calls useAutomationSessions with card id and enabled=true when expanded', () => {
      const useAutoSessions = vi.spyOn(hooks, 'useAutomationSessions').mockReturnValue(
        { data: [] } as unknown as ReturnType<typeof hooks.useAutomationSessions>)
      vi.spyOn(hooks, 'usePatchAutomation').mockReturnValue(
        { mutate: vi.fn() } as unknown as ReturnType<typeof hooks.usePatchAutomation>)
      vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue(
        { data: [] } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
      vi.spyOn(hooks, 'useGithubRepos').mockReturnValue(
        { data: [], isLoading: false } as unknown as ReturnType<typeof hooks.useGithubRepos>)
      vi.spyOn(hooks, 'useAutomationActionable').mockReturnValue(
        { data: [] } as unknown as ReturnType<typeof hooks.useAutomationActionable>)
      vi.spyOn(hooks, 'useAutomationTrigger').mockReturnValue(
        { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useAutomationTrigger>)
      vi.spyOn(hooks, 'useConsolidation').mockReturnValue(
        { data: undefined, isLoading: false } as unknown as ReturnType<typeof hooks.useConsolidation>)
      vi.spyOn(hooks, 'useConsolidate').mockReturnValue(
        { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useConsolidate>)

      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      expect(useAutoSessions).toHaveBeenCalledWith('pr-doctor', true)
    })
  })

  // --- Config panel ---

  describe('Config panel', () => {
    it('renders an input per schema field', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={WITH_SCHEMA} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))

      // bool → checkbox
      const boolCb = screen.getAllByRole('checkbox').find(
        (el) => el.getAttribute('type') === 'checkbox' && (el as HTMLInputElement).checked === true
      )
      expect(boolCb).toBeTruthy()

      // int → number input with seeded value
      const numInput = screen.getByRole('spinbutton')
      expect(numInput).toHaveValue(5)

      // string → text input with seeded value
      const textInput = screen.getByDisplayValue('bot:')
      expect(textInput).toBeInTheDocument()

      // enum → select with seeded value
      const selectEl = screen.getByDisplayValue('thorough')
      expect(selectEl).toBeInTheDocument()

      // repo-multiselect → checkboxes for each repo
      expect(screen.getByLabelText('owner/repo-a')).toBeChecked()
      expect(screen.getByLabelText('owner/repo-b')).not.toBeChecked()
    })

    it('shows field labels', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={WITH_SCHEMA} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      expect(screen.getByText('Max retries')).toBeInTheDocument()
      expect(screen.getByText('Strategy')).toBeInTheDocument()
      expect(screen.getByText('Watched repos')).toBeInTheDocument()
    })

    it('shows help text when present', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={WITH_SCHEMA} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      expect(screen.getByText('Only watch draft PRs')).toBeInTheDocument()
      expect(screen.getByText('Repos to watch')).toBeInTheDocument()
    })

    it('editing an int field calls usePatchAutomation.mutate with the updated config', () => {
      const mutate = vi.fn()
      mockBasicHooks(mutate)
      render(<MemoryRouter><AutomationCard automation={WITH_SCHEMA} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))

      const numInput = screen.getByRole('spinbutton')
      fireEvent.change(numInput, { target: { valueAsNumber: 7 } })

      expect(mutate).toHaveBeenCalledWith({
        id: 'pr-doctor',
        body: {
          config: expect.objectContaining({ max_retries: 7 }),
        },
      })
    })

    it('toggling a repo-multiselect checkbox calls patch with updated array', () => {
      const mutate = vi.fn()
      mockBasicHooks(mutate)
      render(<MemoryRouter><AutomationCard automation={WITH_SCHEMA} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))

      // repo-b is unchecked; click it to add
      fireEvent.click(screen.getByLabelText('owner/repo-b'))

      expect(mutate).toHaveBeenCalledWith({
        id: 'pr-doctor',
        body: {
          config: expect.objectContaining({
            watched_repos: expect.arrayContaining(['owner/repo-a', 'owner/repo-b']),
          }),
        },
      })
    })

    it('does not render config section when configSchema is empty', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      expect(screen.queryByText('Configuration')).not.toBeInTheDocument()
    })
  })

  // --- AutomationActions section ---

  describe('AutomationActions', () => {
    beforeEach(() => {
      vi.mocked(toast.success).mockClear()
      vi.mocked(toast.warning).mockClear()
      vi.mocked(toast.error).mockClear()
    })

    const ITEMS = [
      { id: 'owner/repo#42', title: 'behind PR', subtitle: null, url: 'https://x/42',
        actions: [{ kind: 'out_of_date', label: 'Update branch', style: 'default' }] },
      { id: 'owner/repo#43', title: 'red CI', subtitle: null, url: 'https://x/43',
        actions: [{ kind: 'failing_ci', label: 'Fix CI', style: 'default' }] },
      { id: 'owner/repo#44', title: 'conflicted', subtitle: null, url: 'https://x/44',
        actions: [{ kind: 'merge_conflict', label: 'Resolve conflicts', style: 'risky' }] },
    ]

    function mockActions(mutateAsync: ReturnType<typeof vi.fn>) {
      vi.spyOn(hooks, 'useAutomationActionable').mockReturnValue(
        { data: ITEMS } as unknown as ReturnType<typeof hooks.useAutomationActionable>)
      vi.spyOn(hooks, 'useAutomationTrigger').mockReturnValue(
        { mutateAsync, isPending: false } as unknown as ReturnType<typeof hooks.useAutomationTrigger>)
    }

    function expand() {
      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
    }

    it('renders a button per action and triggers on click', async () => {
      const mutateAsync = vi.fn().mockResolvedValue(
        { status: 'spawned', level: 'ok', message: 'Spawned fix session' })
      mockBasicHooks(); mockActions(mutateAsync)
      expand()
      expect(screen.getByText('red CI')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Fix CI' }))
      expect(mutateAsync).toHaveBeenCalledWith({ item_id: 'owner/repo#43', kind: 'failing_ci' })
      await screen.findByText('Spawned fix session')
      expect(toast.success).toHaveBeenCalledWith('Spawned fix session')
    })

    it('surfaces a warn outcome (usage ceiling) without a success toast', async () => {
      const mutateAsync = vi.fn().mockResolvedValue(
        { status: 'ceiling', level: 'warn', message: 'All accounts at/above usage ceiling — try later' })
      mockBasicHooks(); mockActions(mutateAsync)
      expand()
      fireEvent.click(screen.getByRole('button', { name: 'Fix CI' }))
      await screen.findByText(/usage ceiling/)
      expect(toast.warning).toHaveBeenCalledWith('All accounts at/above usage ceiling — try later')
      expect(toast.success).not.toHaveBeenCalled()
    })

    it('requires a confirm step for a risky action', async () => {
      const mutateAsync = vi.fn().mockResolvedValue(
        { status: 'spawned', level: 'ok', message: 'Spawned fix session' })
      mockBasicHooks(); mockActions(mutateAsync)
      expand()
      fireEvent.click(screen.getByRole('button', { name: 'Resolve conflicts' }))
      expect(mutateAsync).not.toHaveBeenCalled()               // first click = arm confirm
      fireEvent.click(screen.getByRole('button', { name: /Confirm/ }))
      expect(mutateAsync).toHaveBeenCalledWith({ item_id: 'owner/repo#44', kind: 'merge_conflict' })
    })

    it('shows empty state when there are no actionable items', () => {
      vi.spyOn(hooks, 'useAutomationActionable').mockReturnValue(
        { data: [] } as unknown as ReturnType<typeof hooks.useAutomationActionable>)
      vi.spyOn(hooks, 'useAutomationTrigger').mockReturnValue(
        { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useAutomationTrigger>)
      mockBasicHooks()
      expand()
      expect(screen.getByText('No actionable items.')).toBeInTheDocument()
    })
  })
})
