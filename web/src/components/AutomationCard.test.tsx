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

  it('enabling a 2-mode automation with no mode also sets the active mode', () => {
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
    it('shows the active count in the header when > 0', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={{ ...PR_DOCTOR, activeSessions: 3 }} /></MemoryRouter>)
      expect(screen.getByText('3 active')).toBeInTheDocument()
    })

    it('omits the badge when there are no active sessions', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      expect(screen.queryByText(/\bactive\b/)).not.toBeInTheDocument()
    })
  })

  // --- Config panel ---

  describe('Config panel', () => {
    it('does not render an expand button when configSchema is empty', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      expect(screen.queryByRole('button', { name: /expand/i })).not.toBeInTheDocument()
    })

    it('renders an expand button when configSchema has fields', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={WITH_SCHEMA} /></MemoryRouter>)
      expect(screen.getByRole('button', { name: /expand/i })).toBeInTheDocument()
    })

    it('renders an input per schema field when expanded', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={WITH_SCHEMA} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))

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
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))
      expect(screen.getByText('Max retries')).toBeInTheDocument()
      expect(screen.getByText('Strategy')).toBeInTheDocument()
      expect(screen.getByText('Watched repos')).toBeInTheDocument()
    })

    it('shows help text when present', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={WITH_SCHEMA} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))
      expect(screen.getByText('Only watch draft PRs')).toBeInTheDocument()
      expect(screen.getByText('Repos to watch')).toBeInTheDocument()
    })

    it('editing an int field calls usePatchAutomation.mutate with the updated config', () => {
      const mutate = vi.fn()
      mockBasicHooks(mutate)
      render(<MemoryRouter><AutomationCard automation={WITH_SCHEMA} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))

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
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))

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

    it('renders a number input per enabled repo for a repo-number-map field', () => {
      mockBasicHooks()
      const withMap: Automation = {
        ...PR_DOCTOR,
        configSchema: [
          { key: 'enabled_repos', label: 'Repos', type: 'repo-multiselect', default: [], help: '', choices: null },
          { key: 'repo_weights', label: 'Repo weights', type: 'repo-number-map', default: {}, help: 'per-repo weight', choices: null },
        ],
        config: { enabled_repos: ['owner/repo-a', 'owner/repo-b'], repo_weights: { 'owner/repo-a': 3 } },
      }
      render(<MemoryRouter><AutomationCard automation={withMap} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))
      // One numeric input per selected repo; seeded from the map (a=3, b empty).
      // The accessible name is scoped by field label so it never collides with the
      // repo-multiselect checkbox (or the other map) of the same repo name.
      expect(screen.getByLabelText('Repo weights: owner/repo-a')).toHaveValue(3)
      expect(screen.getByLabelText('Repo weights: owner/repo-b')).toHaveValue(null)
    })

    it('editing a repo-number-map input patches the map, not the whole config', () => {
      const mutate = vi.fn()
      mockBasicHooks(mutate)
      const withMap: Automation = {
        ...PR_DOCTOR,
        configSchema: [
          { key: 'enabled_repos', label: 'Repos', type: 'repo-multiselect', default: [], help: '', choices: null },
          { key: 'repo_caps', label: 'Repo caps', type: 'repo-number-map', default: {}, help: '', choices: null },
        ],
        config: { enabled_repos: ['owner/repo-a'], repo_caps: {} },
      }
      render(<MemoryRouter><AutomationCard automation={withMap} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))
      fireEvent.change(screen.getByLabelText('Repo caps: owner/repo-a'), { target: { value: '2' } })
      expect(mutate).toHaveBeenCalledWith({
        id: 'pr-doctor',
        body: { config: expect.objectContaining({ repo_caps: { 'owner/repo-a': 2 } }) },
      })
    })

    it('repo-number-map prompts to select repos when none are enabled', () => {
      mockBasicHooks()
      const withMap: Automation = {
        ...PR_DOCTOR,
        configSchema: [
          { key: 'enabled_repos', label: 'Repos', type: 'repo-multiselect', default: [], help: '', choices: null },
          { key: 'repo_weights', label: 'Repo weights', type: 'repo-number-map', default: {}, help: '', choices: null },
        ],
        config: { enabled_repos: [], repo_weights: {} },
      }
      render(<MemoryRouter><AutomationCard automation={withMap} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))
      expect(screen.getByText('Select repositories above first.')).toBeInTheDocument()
    })

    it('does not render config section when configSchema is empty', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
      expect(screen.queryByText('Configuration')).not.toBeInTheDocument()
    })
  })
  describe('operator-triggered origins', () => {
    // An origin with no control loop (`modes: []`): registered for its audited
    // engine ceiling and its config row, spawned only when something calls its
    // seam. A toggle here would be a switch that switches nothing (#1268/#1269).
    const PM_CLASSIFY: Automation = {
      id: 'pm-classify', name: 'PM Classification', description: 'triage proposals',
      enabled: false, mode: null, modes: [], operatorTriggered: true,
      status: { text: 'operator-triggered', level: 'idle', counts: null },
      enginePolicy: 'codex_allowed',
      configSchema: [
        { key: 'engine', label: 'Agent engine', type: 'enum', default: 'claude_code', help: '', choices: ['claude_code', 'codex'] },
      ],
      config: {}, lastActiveAt: null, activeSessions: 0,
    }

    it('renders no enable toggle', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={PM_CLASSIFY} /></MemoryRouter>)
      expect(screen.getByText('PM Classification')).toBeInTheDocument()
      expect(screen.queryByRole('switch')).not.toBeInTheDocument()
      expect(screen.getByText(/triggered/i)).toBeInTheDocument()
    })

    it('still exposes the engine selector its ceiling admits', () => {
      const mutate = vi.fn()
      mockBasicHooks(mutate)
      render(<MemoryRouter><AutomationCard automation={PM_CLASSIFY} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))
      expect(screen.getByLabelText('Agent engine')).toBeInTheDocument()
    })

    it('offers no schedule editor for an origin that never ticks', () => {
      mockBasicHooks()
      render(<MemoryRouter><AutomationCard automation={PM_CLASSIFY} /></MemoryRouter>)
      fireEvent.click(screen.getByRole('button', { name: /expand/i }))
      expect(screen.queryByText('Schedule')).not.toBeInTheDocument()
    })
  })
})
