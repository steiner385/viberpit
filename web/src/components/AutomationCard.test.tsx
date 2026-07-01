// web/src/components/AutomationCard.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AutomationCard } from './AutomationCard'
import * as hooks from '@/api/hooks'
import type { Automation } from '@/api/automations'

const PR_DOCTOR: Automation = {
  id: 'pr-doctor', name: 'PR Doctor', description: 'watches CI',
  enabled: false, mode: 'propose', modes: ['off', 'auto', 'propose'],
  status: { text: 'idle', level: 'idle', counts: null },
  configSchema: [], config: {}, lastActiveAt: null,
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

  // --- Spawned sessions section ---

  describe('Spawned sessions', () => {
    it('shows empty state when no sessions', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      // Expand the card
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      expect(screen.getByText('No sessions spawned.')).toBeInTheDocument()
    })

    it('renders a session row with a router Link when data is present', () => {
      vi.spyOn(hooks, 'usePatchAutomation').mockReturnValue(
        { mutate: vi.fn() } as unknown as ReturnType<typeof hooks.usePatchAutomation>)
      vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue(
        { data: [] } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
      vi.spyOn(hooks, 'useAutomationSessions').mockReturnValue({
        data: [{ id: 'sess-1', name: 'Fix PR #42', status: 'in_progress', originRef: 'pr/42' }],
      } as unknown as ReturnType<typeof hooks.useAutomationSessions>)
      vi.spyOn(hooks, 'useGithubRepos').mockReturnValue(
        { data: [], isLoading: false } as unknown as ReturnType<typeof hooks.useGithubRepos>)

      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))

      const link = screen.getByRole('link', { name: 'Fix PR #42' })
      expect(link).toBeInTheDocument()
      expect(link).toHaveAttribute('href', '/session/sess-1')
      expect(screen.getByText('· in_progress')).toBeInTheDocument()
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
})
