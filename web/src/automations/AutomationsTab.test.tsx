import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AutomationsTab } from './AutomationsTab'
import * as hooks from '@/api/hooks'
import type { Automation, PrDoctorJob } from '@/api/automations'
import { fetchPrDoctorJob } from '@/api/automations'
import { toast } from 'sonner'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}))

// Async trigger job polling (issue #668): stub the job endpoint, keep the rest real.
vi.mock('@/api/automations', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/automations')>()
  return { ...actual, fetchPrDoctorJob: vi.fn() }
})
const mockFetchJob = vi.mocked(fetchPrDoctorJob)

function job(over: Partial<PrDoctorJob>): PrDoctorJob {
  return {
    job_id: 'prd-x', target: 'owner/repo#43', kind: 'failing_ci',
    status: 'running', phase: 'cloning', session_id: null,
    error_phase: null, message: '', level: 'ok', ...over,
  }
}

const PR_DOCTOR: Automation = {
  id: 'pr-doctor', name: 'PR Doctor', description: 'watches CI',
  enabled: true, mode: 'auto', modes: ['off', 'auto', 'propose'],
  status: { text: 'idle', level: 'idle', counts: null },
  configSchema: [], config: {}, lastActiveAt: null, activeSessions: 0,
}

const CICD: Automation = {
  id: 'cicd-doctor', name: 'CI/CD Doctor', description: '',
  enabled: false, mode: 'propose', modes: ['off', 'propose'],
  status: { text: 'active', level: 'active', counts: null },
  configSchema: [], config: {}, lastActiveAt: null, activeSessions: 2,
}

function mockBasicHooks() {
  vi.spyOn(hooks, 'useAutomations').mockReturnValue(
    { data: [PR_DOCTOR], isLoading: false } as unknown as ReturnType<typeof hooks.useAutomations>)
  vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue(
    { data: [] } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
  vi.spyOn(hooks, 'useAutomationSessions').mockReturnValue(
    { data: [] } as unknown as ReturnType<typeof hooks.useAutomationSessions>)
  vi.spyOn(hooks, 'useAutomationActionable').mockReturnValue(
    { data: [] } as unknown as ReturnType<typeof hooks.useAutomationActionable>)
  vi.spyOn(hooks, 'useAutomationTrigger').mockReturnValue(
    { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useAutomationTrigger>)
  vi.spyOn(hooks, 'useConsolidation').mockReturnValue(
    { data: undefined, isLoading: false } as unknown as ReturnType<typeof hooks.useConsolidation>)
  vi.spyOn(hooks, 'useConsolidate').mockReturnValue(
    { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useConsolidate>)
}

describe('AutomationsTab', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders an automation card per automation', () => {
    vi.spyOn(hooks, 'useAutomations').mockReturnValue(
      { data: [PR_DOCTOR, CICD], isLoading: false } as unknown as ReturnType<typeof hooks.useAutomations>)
    mockBasicHooks()
    vi.spyOn(hooks, 'useAutomations').mockReturnValue(
      { data: [PR_DOCTOR, CICD], isLoading: false } as unknown as ReturnType<typeof hooks.useAutomations>)
    render(<MemoryRouter><AutomationsTab /></MemoryRouter>)
    expect(screen.getByText('PR Doctor')).toBeInTheDocument()
    expect(screen.getByText('CI/CD Doctor')).toBeInTheDocument()
  })

  it('shows loading state', () => {
    vi.spyOn(hooks, 'useAutomations').mockReturnValue(
      { data: undefined, isLoading: true } as unknown as ReturnType<typeof hooks.useAutomations>)
    render(<MemoryRouter><AutomationsTab /></MemoryRouter>)
    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })

  it('shows empty state when no automations', () => {
    vi.spyOn(hooks, 'useAutomations').mockReturnValue(
      { data: [], isLoading: false } as unknown as ReturnType<typeof hooks.useAutomations>)
    render(<MemoryRouter><AutomationsTab /></MemoryRouter>)
    expect(screen.getByText(/no automations configured/i)).toBeInTheDocument()
  })

  it('shows the active sessions badge', () => {
    mockBasicHooks()
    vi.spyOn(hooks, 'useAutomations').mockReturnValue(
      { data: [{ ...PR_DOCTOR, activeSessions: 3 }], isLoading: false } as unknown as ReturnType<typeof hooks.useAutomations>)
    render(<MemoryRouter><AutomationsTab /></MemoryRouter>)
    expect(screen.getByText('3 active')).toBeInTheDocument()
  })

  it('card is collapsed by default, expand shows live sections', () => {
    mockBasicHooks()
    render(<MemoryRouter><AutomationsTab /></MemoryRouter>)
    expect(screen.queryByText('Activity')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
    // "Activity" heading (exact) and "No activity yet." are both present after expand
    expect(screen.getByText('Activity')).toBeInTheDocument()
    expect(screen.getByText('No activity yet.')).toBeInTheDocument()
    expect(screen.getByText('No actionable items.')).toBeInTheDocument()
    expect(screen.getByText('No sessions spawned.')).toBeInTheDocument()
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
      render(<MemoryRouter><AutomationsTab onOpenSession={onOpenSession} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      fireEvent.click(screen.getByRole('button', { name: 'spawned fixer' }))
      expect(onOpenSession).toHaveBeenCalledWith('sess-9')
    })
  })

  // --- Spawned sessions section ---

  describe('Spawned sessions', () => {
    it('shows empty state when no sessions', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationsTab /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      expect(screen.getByText('No sessions spawned.')).toBeInTheDocument()
    })

    it('opens a spawned session via onOpenSession when clicked', () => {
      const onOpenSession = vi.fn()
      mockBasicHooks()
      vi.spyOn(hooks, 'useAutomationSessions').mockReturnValue({
        data: [{ id: 'sess-1', name: 'Fix PR #42', status: 'in_progress', originRef: 'pr/42' }],
      } as unknown as ReturnType<typeof hooks.useAutomationSessions>)
      render(<MemoryRouter><AutomationsTab onOpenSession={onOpenSession} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      fireEvent.click(screen.getByRole('button', { name: 'Fix PR #42' }))
      expect(onOpenSession).toHaveBeenCalledWith('sess-1')
    })

    it('reviews an archived spawned session via onReviewSession', () => {
      const onOpenSession = vi.fn()
      const onReviewSession = vi.fn()
      mockBasicHooks()
      vi.spyOn(hooks, 'useAutomationSessions').mockReturnValue({
        data: [{ id: 'sess-2', name: 'Implement issue 16', status: 'archived', originRef: 'owner/repo#16' }],
      } as unknown as ReturnType<typeof hooks.useAutomationSessions>)
      render(<MemoryRouter><AutomationsTab onOpenSession={onOpenSession} onReviewSession={onReviewSession} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      fireEvent.click(screen.getByRole('button', { name: 'Implement issue 16' }))
      expect(onReviewSession).toHaveBeenCalledWith('sess-2')
      expect(onOpenSession).not.toHaveBeenCalled()
    })

    it('passes automation id and enabled=true to useAutomationSessions when expanded', () => {
      const useAutoSessions = vi.spyOn(hooks, 'useAutomationSessions').mockReturnValue(
        { data: [] } as unknown as ReturnType<typeof hooks.useAutomationSessions>)
      mockBasicHooks()
      render(<MemoryRouter><AutomationsTab /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      expect(useAutoSessions).toHaveBeenCalledWith('pr-doctor', true)
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
      render(<MemoryRouter><AutomationsTab /></MemoryRouter>)
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
      expect(mutateAsync).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: /Confirm/ }))
      expect(mutateAsync).toHaveBeenCalledWith({ item_id: 'owner/repo#44', kind: 'merge_conflict' })
    })

    it('cancel button disarms an armed risky confirm (#466)', async () => {
      const mutateAsync = vi.fn()
      mockBasicHooks(); mockActions(mutateAsync)
      expand()
      fireEvent.click(screen.getByRole('button', { name: 'Resolve conflicts' }))
      expect(screen.getByRole('button', { name: /Confirm/ })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Cancel Resolve conflicts' }))
      expect(screen.getByRole('button', { name: 'Resolve conflicts' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /Confirm/ })).not.toBeInTheDocument()
      expect(mutateAsync).not.toHaveBeenCalled()
    })

    it('auto-disarms an armed risky confirm after the timeout (#466)', async () => {
      vi.useFakeTimers()
      try {
        const mutateAsync = vi.fn()
        mockBasicHooks(); mockActions(mutateAsync)
        expand()
        fireEvent.click(screen.getByRole('button', { name: 'Resolve conflicts' }))
        expect(screen.getByRole('button', { name: /Confirm/ })).toBeInTheDocument()
        act(() => { vi.advanceTimersByTime(4000) })
        expect(screen.getByRole('button', { name: 'Resolve conflicts' })).toBeInTheDocument()
        expect(screen.queryByRole('button', { name: /Confirm/ })).not.toBeInTheDocument()
        expect(mutateAsync).not.toHaveBeenCalled()
      } finally {
        vi.useRealTimers()
      }
    })

    it('async trigger (#668): shows the live phase then settles from the job', async () => {
      let calls = 0
      mockFetchJob.mockImplementation(async () => {
        calls += 1
        return calls === 1
          ? job({ status: 'running', phase: 'cloning' })
          : job({ status: 'ready', phase: 'ready', session_id: 'sess-9',
                  level: 'ok', message: 'Spawned fix session' })
      })
      const mutateAsync = vi.fn().mockResolvedValue(
        { status: 'queued', level: 'ok', message: 'Queued…', job_id: 'prd-x', phase: 'queued' })
      mockBasicHooks(); mockActions(mutateAsync)
      expand()
      fireEvent.click(screen.getByRole('button', { name: 'Fix CI' }))
      // The button shows the live phase while the background pipeline runs…
      await screen.findByRole('button', { name: 'Cloning…' })
      expect(mockFetchJob).toHaveBeenCalledWith('prd-x')
      // …and settles to the job's terminal outcome + toast (no premature toast).
      await screen.findByText('Spawned fix session')
      expect(toast.success).toHaveBeenCalledWith('Spawned fix session')
    })

    it('async trigger (#668): surfaces a failed phase from the job', async () => {
      mockFetchJob.mockResolvedValue(
        job({ status: 'error', phase: 'cloning', error_phase: 'cloning',
              level: 'error', message: 'Clone failed — see the automation feed' }))
      const mutateAsync = vi.fn().mockResolvedValue(
        { status: 'queued', level: 'ok', message: 'Queued…', job_id: 'prd-x', phase: 'queued' })
      mockBasicHooks(); mockActions(mutateAsync)
      expand()
      fireEvent.click(screen.getByRole('button', { name: 'Fix CI' }))
      await screen.findByText('Clone failed — see the automation feed')
      expect(toast.error).toHaveBeenCalledWith('Clone failed — see the automation feed')
      expect(toast.success).not.toHaveBeenCalled()
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
