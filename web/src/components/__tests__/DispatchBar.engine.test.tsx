/**
 * DispatchBar — the Agent selector and everything it filters.
 *
 * The bar is the one place a user chooses which agent runs a session, so the
 * invariants pinned here are the ones that would silently launch the WRONG
 * thing: an engine the server no longer offers, an account belonging to the
 * other engine, or a stale model/reasoning pick left visible after the engine
 * changed underneath it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DispatchBar } from '../DispatchBar'
import { ENGINE_PREFERENCE_KEY } from '@/lib/enginePreference'
import type { AgentCapabilities, EngineOffer, ModelOption } from '@/api/types'
import { account, fleetHost, githubRepo } from '@/test/factories'
import type { HostEngineState } from '@/api/fleet'

function caps(over: Partial<AgentCapabilities> = {}): AgentCapabilities {
  return {
    models: true, reasoningEffort: false, steer: true, interrupt: true,
    approvals: true, questions: true, fork: true, adopt: true,
    selfModelSwitch: true, subagents: true, attachments: true, accountRotation: true,
    accountSwitchNewThread: true,
    ...over,
  }
}

function offer(over: Partial<EngineOffer> = {}): EngineOffer {
  return {
    engine: 'claude_code',
    available: true,
    hosts: ['edge'],
    unavailableReasons: {},
    blockedReason: null,
    capabilities: caps(),
    ...over,
  }
}

const BOTH_ENGINES: EngineOffer[] = [
  offer(),
  offer({ engine: 'codex', capabilities: caps({ reasoningEffort: true }) }),
]

function hostEngineState(over: Partial<HostEngineState> = {}): HostEngineState {
  return {
    engine: 'codex', available: true, reason: null, cliVersion: '0.9.0',
    contractSha256: 'abc', externalAuth: true, dispatchable: true, dispatchReason: null,
    ...over,
  }
}

/** What a probed host really carries: ONE entry per engine the edge knows about
 *  — never only the ones the runner mentioned (`server/api/fleet.py:_engines_wire`). */
function hostEngines(codex: Partial<HostEngineState> = {}): HostEngineState[] {
  return [
    hostEngineState({ engine: 'claude_code' }),
    hostEngineState({ engine: 'codex', ...codex }),
  ]
}

const ACCOUNTS = [
  account({ name: 'work', nickname: 'Work', engine: 'claude_code' }),
  account({ name: 'chatgpt', nickname: null, engine: 'codex', provider: 'openai' }),
  // No `engine` at all: every pre-Codex account row, which must stay Claude.
  account({ name: 'legacy', nickname: null, engine: undefined }),
]

const CLAUDE_MODELS: ModelOption[] = [
  { id: 'default', label: 'Default' },
  { id: 'opus', label: 'Opus' },
]

const CODEX_MODELS: ModelOption[] = [
  {
    id: 'gpt-5-codex', label: 'GPT-5 Codex', default: true,
    reasoningEfforts: ['low', 'medium', 'high'], defaultReasoningEffort: 'medium',
  },
  { id: 'gpt-5-mini', label: 'GPT-5 mini' },
]

function makeProps(overrides: Partial<React.ComponentProps<typeof DispatchBar>> = {}) {
  return {
    repos: [githubRepo()],
    reposLoading: false,
    reposError: false,
    accounts: ACCOUNTS,
    models: CLAUDE_MODELS,
    engines: BOTH_ENGINES,
    onDispatch: vi.fn().mockResolvedValue(undefined),
    onNavigate: vi.fn(),
    onOpenHighlighted: vi.fn(),
    onEnterList: vi.fn(),
    onJump: vi.fn(),
    onShowHelp: vi.fn(),
    onEscape: vi.fn(),
    isPending: false,
    ...overrides,
  }
}

const agentSelect = () => screen.getByRole('combobox', { name: 'Agent' })
const accountSelect = () => screen.getByRole('combobox', { name: 'Account' })
const modelSelect = () => screen.getByRole('combobox', { name: 'Model' })
const optionValues = (el: HTMLElement) =>
  Array.from(el.querySelectorAll('option')).map((o) => o.value)

beforeEach(() => {
  window.localStorage.clear()
  vi.clearAllMocks()
})

// ── the selector itself ──────────────────────────────────────────────────────

describe('DispatchBar — the Agent selector', () => {
  it('is absent when the server offers a single engine', () => {
    render(<DispatchBar {...makeProps({ engines: [offer()] })} />)
    expect(screen.queryByRole('combobox', { name: 'Agent' })).not.toBeInTheDocument()
  })

  it('is absent on an edge that reports no engines at all (pre-Codex)', () => {
    render(<DispatchBar {...makeProps({ engines: [] })} />)
    expect(screen.queryByRole('combobox', { name: 'Agent' })).not.toBeInTheDocument()
  })

  it('offers both engines by name once the server knows about two', () => {
    render(<DispatchBar {...makeProps()} />)
    expect(optionValues(agentSelect())).toEqual(['claude_code', 'codex'])
    expect(screen.getByRole('option', { name: 'Claude Code' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /^Codex/ })).toBeInTheDocument()
  })

  it('defaults to Claude Code until the user has explicitly chosen', () => {
    render(<DispatchBar {...makeProps()} />)
    expect(agentSelect()).toHaveValue('claude_code')
  })

  it('persists an explicit choice and restores it next mount', async () => {
    const user = userEvent.setup()
    const { unmount } = render(<DispatchBar {...makeProps()} />)
    await user.selectOptions(agentSelect(), 'codex')
    expect(window.localStorage.getItem(ENGINE_PREFERENCE_KEY)).toBe('codex')
    unmount()

    render(<DispatchBar {...makeProps()} />)
    expect(agentSelect()).toHaveValue('codex')
  })

  it('never selects a persisted engine the server no longer offers', () => {
    window.localStorage.setItem(ENGINE_PREFERENCE_KEY, 'codex')
    render(<DispatchBar {...makeProps({
      engines: [offer(), offer({ engine: 'codex', available: false, hosts: [] })],
    })} />)
    expect(agentSelect()).toHaveValue('claude_code')
  })

  it('shows WHY an unavailable engine cannot be picked, and disables it', () => {
    window.localStorage.setItem(ENGINE_PREFERENCE_KEY, 'codex')
    render(<DispatchBar {...makeProps({
      engines: [offer(), offer({
        engine: 'codex', available: false, hosts: [],
        unavailableReasons: {}, blockedReason: 'authority_unconfigured',
      })],
    })} />)
    const codex = screen.getByRole('option', { name: /Codex/ }) as HTMLOptionElement
    expect(codex).toBeDisabled()
    expect(codex.text).toContain('no credential authority is configured')
  })

  it('an edge-side blocker is reported instead of per-runner reasons', () => {
    // `blockedReason` means no runner is at fault, so naming one would send the
    // operator to the wrong machine.
    render(<DispatchBar {...makeProps({
      engines: [offer(), offer({
        engine: 'codex', available: false, hosts: [],
        unavailableReasons: {}, blockedReason: 'authority_unconfigured',
      })],
    })} />)
    const codex = screen.getByRole('option', { name: /Codex/ }) as HTMLOptionElement
    expect(codex.text).not.toMatch(/runner/)
  })

  it('names the runners at fault when the edge itself is fine', () => {
    render(<DispatchBar {...makeProps({
      engines: [offer(), offer({
        engine: 'codex', available: false, hosts: [],
        unavailableReasons: { rocky: 'binary_not_found' }, blockedReason: null,
      })],
    })} />)
    const codex = screen.getByRole('option', { name: /Codex/ }) as HTMLOptionElement
    expect(codex.text).toContain('no runner')
  })

  it('carries the chosen engine into the dispatch payload', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch, models: CODEX_MODELS })} />)

    await user.selectOptions(agentSelect(), 'codex')
    await user.selectOptions(accountSelect(), 'chatgpt')
    await user.type(screen.getByLabelText('New session prompt'), 'ship it')
    await user.keyboard('{Enter}')

    expect(onDispatch).toHaveBeenCalledWith(
      expect.objectContaining({ engine: 'codex', account: 'chatgpt' }))
  })

  it('sends engine: claude_code when no selector is offered at all', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch, engines: [] })} />)
    await user.type(screen.getByLabelText('New session prompt'), 'go')
    await user.keyboard('{Enter}')
    expect(onDispatch).toHaveBeenCalledWith(
      expect.objectContaining({ engine: 'claude_code', reasoningEffort: null }))
  })
})

// ── the engine filters the other pickers ─────────────────────────────────────

describe('DispatchBar — the engine filters account, model and runner', () => {
  it('offers only accounts that belong to the selected engine', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps()} />)

    // Claude: the claude account and the engine-less legacy row, never the OpenAI one.
    expect(optionValues(accountSelect())).toEqual(['', 'work', 'legacy'])

    await user.selectOptions(agentSelect(), 'codex')
    expect(optionValues(accountSelect())).toEqual(['', 'chatgpt'])
  })

  it('VISIBLY clears an account that does not belong to the new engine', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps()} />)
    await user.selectOptions(accountSelect(), 'work')
    expect(accountSelect()).toHaveValue('work')

    await user.selectOptions(agentSelect(), 'codex')
    // Reset to the auto sentinel — not merely hidden behind a filtered list,
    // which would leave `work` in the dispatch payload for a Codex launch.
    await waitFor(() => expect(accountSelect()).toHaveValue(''))
  })

  it('clears model and runner picks too', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({
      hosts: [fleetHost({ name: 'edge', engines: hostEngines() }),
        fleetHost({ name: 'rocky', engines: hostEngines() })],
    })} />)
    await user.selectOptions(modelSelect(), 'opus')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Runner' }), 'rocky')

    await user.selectOptions(agentSelect(), 'codex')

    // The Claude model pick is gone — Codex with no account yet shows the
    // "select an account first" sentinel rather than the previous engine's model.
    await waitFor(() => expect(modelSelect()).not.toHaveValue('opus'))
    expect(screen.getByRole('option', { name: /select an account/i })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Runner' })).toHaveValue('')
  })

  it('a cleared model reaches the backend as null, not as the previous engine\'s pick', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch, models: CODEX_MODELS })} />)
    await user.selectOptions(agentSelect(), 'codex')
    await user.selectOptions(accountSelect(), 'chatgpt')
    await user.selectOptions(modelSelect(), 'gpt-5-codex')

    await user.selectOptions(agentSelect(), 'claude_code')
    await user.type(screen.getByLabelText('New session prompt'), 'go')
    await user.keyboard('{Enter}')

    expect(onDispatch).toHaveBeenCalledWith(expect.objectContaining({
      engine: 'claude_code', account: null, model: null, reasoningEffort: null,
    }))
  })

  it('offers only runners that can dispatch the selected engine', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({
      hosts: [
        fleetHost({ name: 'edge', engines: hostEngines({ dispatchable: true }) }),
        fleetHost({
          name: 'rocky',
          engines: hostEngines({
            available: true, dispatchable: false, dispatchReason: 'contract_hash_mismatch',
          }),
        }),
      ],
    })} />)

    await user.selectOptions(agentSelect(), 'codex')
    expect(optionValues(screen.getByRole('combobox', { name: 'Runner' }))).toEqual(['', 'edge'])
  })

  it('hides the Runner dropdown when no host can run the selected engine', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({
      hosts: [fleetHost({
        name: 'edge',
        engines: hostEngines({ available: false, reason: 'binary_not_found', dispatchable: false }),
      })],
    })} />)
    expect(screen.getByRole('combobox', { name: 'Runner' })).toBeInTheDocument()
    await user.selectOptions(agentSelect(), 'codex')
    expect(screen.queryByRole('combobox', { name: 'Runner' })).not.toBeInTheDocument()
  })

  it('treats a host with no engine information as Claude-only (rolling upgrade)', () => {
    render(<DispatchBar {...makeProps({ hosts: [fleetHost({ name: 'legacy-runner' })] })} />)
    expect(optionValues(screen.getByRole('combobox', { name: 'Runner' })))
      .toEqual(['', 'legacy-runner'])
  })
})

// ── models need an account for Codex ─────────────────────────────────────────

describe('DispatchBar — the Codex model list needs an account', () => {
  it('asks for an account instead of offering a model list', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({ models: [] })} />)
    await user.selectOptions(agentSelect(), 'codex')

    expect(modelSelect()).toBeDisabled()
    expect(screen.getByRole('option', { name: /select an account/i })).toBeInTheDocument()
  })

  it('tells the dock which engine/account to fetch models for', async () => {
    const user = userEvent.setup()
    const onEngineContextChange = vi.fn()
    render(<DispatchBar {...makeProps({ onEngineContextChange })} />)

    expect(onEngineContextChange).toHaveBeenCalledWith({ engine: 'claude_code', account: null })

    await user.selectOptions(agentSelect(), 'codex')
    await user.selectOptions(accountSelect(), 'chatgpt')

    await waitFor(() => expect(onEngineContextChange)
      .toHaveBeenLastCalledWith({ engine: 'codex', account: 'chatgpt' }))
  })
})

// ── reasoning effort ─────────────────────────────────────────────────────────

describe('DispatchBar — the reasoning-effort selector', () => {
  async function pickCodexModel(user: ReturnType<typeof userEvent.setup>) {
    await user.selectOptions(agentSelect(), 'codex')
    await user.selectOptions(accountSelect(), 'chatgpt')
    await user.selectOptions(modelSelect(), 'gpt-5-codex')
  }

  it('is absent for a model that advertises no efforts', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({ models: CODEX_MODELS })} />)
    await user.selectOptions(agentSelect(), 'codex')
    await user.selectOptions(accountSelect(), 'chatgpt')
    await user.selectOptions(modelSelect(), 'gpt-5-mini')
    expect(screen.queryByRole('combobox', { name: /reasoning/i })).not.toBeInTheDocument()
  })

  it('appears for a model that does, initialised to the advertised default', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({ models: CODEX_MODELS })} />)
    await pickCodexModel(user)
    const reasoning = screen.getByRole('combobox', { name: /reasoning/i })
    expect(reasoning).toHaveValue('medium')
    expect(optionValues(reasoning)).toEqual(['low', 'medium', 'high'])
  })

  it('stays hidden when the engine does not advertise the capability', async () => {
    // Capabilities, not `if (engine === 'codex')`, decide which controls render.
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({
      models: CODEX_MODELS,
      engines: [offer(), offer({ engine: 'codex', capabilities: caps({ reasoningEffort: false }) })],
    })} />)
    await pickCodexModel(user)
    expect(screen.queryByRole('combobox', { name: /reasoning/i })).not.toBeInTheDocument()
  })

  it('threads the chosen effort into the dispatch payload', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ models: CODEX_MODELS, onDispatch })} />)
    await pickCodexModel(user)
    await user.selectOptions(screen.getByRole('combobox', { name: /reasoning/i }), 'high')

    await user.type(screen.getByLabelText('New session prompt'), 'go')
    await user.keyboard('{Enter}')

    expect(onDispatch).toHaveBeenCalledWith(expect.objectContaining({
      engine: 'codex', account: 'chatgpt', model: 'gpt-5-codex', reasoningEffort: 'high',
    }))
  })

  it('re-initialises the effort when the model changes', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({
      models: [
        ...CODEX_MODELS,
        { id: 'gpt-5-pro', label: 'GPT-5 Pro', reasoningEfforts: ['low', 'high'], defaultReasoningEffort: 'high' },
      ],
    })} />)
    await pickCodexModel(user)
    await user.selectOptions(screen.getByRole('combobox', { name: /reasoning/i }), 'low')
    await user.selectOptions(modelSelect(), 'gpt-5-pro')
    expect(screen.getByRole('combobox', { name: /reasoning/i })).toHaveValue('high')
  })
})

// ── mobile disclosure ────────────────────────────────────────────────────────

describe('DispatchBar — the new controls join the mobile disclosure', () => {
  it('a non-default agent lights the collapsed indicator', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps()} />)
    const more = screen.getByRole('button', { name: /more options/i })
    await user.click(more)
    await user.selectOptions(agentSelect(), 'codex')
    await user.click(more)
    expect(screen.getByText(/non-default agent, account, model, reasoning, or runner selected/i))
      .toBeInTheDocument()
  })

  it('aria-controls lists every disclosed select that is actually rendered', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({
      models: CODEX_MODELS,
      hosts: [fleetHost({ name: 'edge', engines: hostEngines() })],
    })} />)
    const more = screen.getByRole('button', { name: /more options/i })
    await user.selectOptions(agentSelect(), 'codex')
    await user.selectOptions(accountSelect(), 'chatgpt')
    await user.selectOptions(modelSelect(), 'gpt-5-codex')

    const controlled = (more.getAttribute('aria-controls') ?? '').split(' ').filter(Boolean)
    expect(controlled).toEqual([
      'dispatch-bar-engine', 'dispatch-bar-account', 'dispatch-bar-model',
      'dispatch-bar-reasoning', 'dispatch-bar-runner',
    ])
    // Every id must resolve — a dangling aria-controls is an a11y violation.
    for (const id of controlled) expect(document.getElementById(id)).not.toBeNull()
  })

  it('every new control meets the 44px mobile target', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({ models: CODEX_MODELS })} />)
    await user.selectOptions(agentSelect(), 'codex')
    await user.selectOptions(accountSelect(), 'chatgpt')
    await user.selectOptions(modelSelect(), 'gpt-5-codex')
    expect(agentSelect().className).toContain('min-h-[44px]')
    expect(screen.getByRole('combobox', { name: /reasoning/i }).className).toContain('min-h-[44px]')
  })

  it('labels the new selects with a real <label htmlFor>', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps({ models: CODEX_MODELS })} />)
    await user.selectOptions(agentSelect(), 'codex')
    await user.selectOptions(accountSelect(), 'chatgpt')
    await user.selectOptions(modelSelect(), 'gpt-5-codex')
    for (const id of ['dispatch-bar-engine', 'dispatch-bar-reasoning']) {
      expect(document.querySelector(`label[for="${id}"]`)).not.toBeNull()
    }
  })
})
