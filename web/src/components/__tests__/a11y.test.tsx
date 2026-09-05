/**
 * Automated WCAG 2.1 AA accessibility audit for management surfaces.
 *
 * Uses jest-axe (axe-core) to catch labelling, role, focus, and contrast-attribute
 * violations. The xterm.js Terminal component is intentionally excluded — it carries
 * a documented partial-conformance note (see docs/ACCESSIBILITY.md).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { AddAccountDialog } from '../AddAccountDialog'
import { AllLimitedBanner } from '../AllLimitedBanner'
import { DispatchBar } from '../DispatchBar'
import { EmptyState } from '../EmptyState'
import { SessionList } from '../SessionList'
import { UsageBadge, StatusPill } from '../UsageBadge'
import type { AgentCapabilities, EngineOffer, ModelOption, Session, Account } from '@/api/types'
import { anthropicUsage, account, codexUsage, githubRepo, session } from '@/test/factories'

expect.extend(toHaveNoViolations)

// ── test data ──────────────────────────────────────────────────────────────

const ACCOUNTS: Account[] = [
  account({
    name: 'alice',
    status: 'ok',
    usage: anthropicUsage(30, 50),
  }),
  account({
    name: 'bob',
    status: 'limited',
    usage: anthropicUsage(95, 80),
  }),
  account({
    name: 'chatgpt',
    status: 'ok',
    provider: 'openai',
    engine: 'codex',
    usage: codexUsage(20),
  }),
]

function capabilities(over: Partial<AgentCapabilities> = {}): AgentCapabilities {
  return {
    models: true, reasoningEffort: false, steer: true, interrupt: true,
    approvals: true, questions: true, fork: true, adopt: true,
    selfModelSwitch: true, subagents: true, attachments: true, accountRotation: true,
    ...over,
  }
}

function engineOffer(over: Partial<EngineOffer> = {}): EngineOffer {
  return {
    engine: 'claude_code',
    available: true,
    hosts: ['edge'],
    unavailableReasons: {},
    blockedReason: null,
    capabilities: capabilities(),
    ...over,
  }
}

const SESSIONS: Session[] = [
  session({
    id: 'sess-1',
    name: 'Work session',
    accountName: 'alice',
    status: 'in_progress',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  }),
  session({
    id: 'sess-2',
    name: 'Research',
    accountName: 'bob',
    status: 'idle',
    createdAt: '2026-06-19T11:00:00Z',
    updatedAt: '2026-06-19T11:00:00Z',
  }),
]

function qcWrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

// ── AllLimitedBanner ───────────────────────────────────────────────────────

describe('a11y: AllLimitedBanner', () => {
  it('has no axe violations with a reset time', async () => {
    const { container } = render(
      <AllLimitedBanner earliestReset="2026-06-19T18:00:00Z" onDismiss={vi.fn()} />,
    )
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })

  it('has no axe violations with null reset', async () => {
    const { container } = render(
      <AllLimitedBanner earliestReset={null} onDismiss={vi.fn()} />,
    )
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })
})

// ── EmptyState ─────────────────────────────────────────────────────────────

describe('a11y: EmptyState', () => {
  it('has no axe violations', async () => {
    const { container } = render(<EmptyState />)
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })
})

// ── SessionList ────────────────────────────────────────────────────────────

describe('a11y: SessionList', () => {
  const handlers = {
    onOpen: vi.fn(),
    onRename: vi.fn(),
    onClose: vi.fn(),
    onDelete: vi.fn(),
    onQuickResume: vi.fn(),
  }

  it('has no axe violations (empty)', async () => {
    const { container } = render(
      <SessionList sessions={[]} accounts={[]} {...handlers} />,
      { wrapper: qcWrapper },
    )
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })

  it('has no axe violations (loading)', async () => {
    const { container } = render(
      <SessionList sessions={[]} accounts={[]} isLoading {...handlers} />,
      { wrapper: qcWrapper },
    )
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })

  it('has no axe violations (with sessions)', async () => {
    const { container } = render(
      <SessionList sessions={SESSIONS} accounts={ACCOUNTS} {...handlers} />,
      { wrapper: qcWrapper },
    )
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })
})

// ── UsageBadge ─────────────────────────────────────────────────────────────

describe('a11y: UsageBadge', () => {
  it('has no axe violations at low usage', async () => {
    const { container } = render(<UsageBadge pct={20} label="session" />)
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })

  it('has no axe violations at high usage', async () => {
    const { container } = render(<UsageBadge pct={95} label="weekly" />)
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })

  it('has no axe violations without a label', async () => {
    const { container } = render(<UsageBadge pct={50} />)
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })
})

// ── StatusPill ─────────────────────────────────────────────────────────────

describe('a11y: StatusPill', () => {
  const statuses = ['ok', 'limited', 'needs-relogin', 'not-max', 'unknown'] as const

  it.each(statuses)('has no axe violations for account status "%s"', async (status) => {
    const { container } = render(<StatusPill status={status} />)
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })

  it('has no axe violations for session statuses', async () => {
    const sessionStatuses = ['in_progress', 'idle', 'limited', 'ended'] as const
    for (const status of sessionStatuses) {
      const { container } = render(<StatusPill status={status} />)
      const results = await axe(container)
      expect(results).toHaveNoViolations()
    }
  })
})

// ── DispatchBar ────────────────────────────────────────────────────────────
//
// The engine-aware controls (Agent, Reasoning) are conditional, so both shapes
// are audited: with a single engine offered nothing new renders at all, and with
// two the Agent select, the filtered Account list and the Reasoning select must
// all carry names — plus `aria-controls` on the ⋯ toggle must not point at an id
// that is not on the page.

const CLAUDE_ONLY: EngineOffer[] = [engineOffer()]
const BOTH_ENGINES: EngineOffer[] = [
  engineOffer(),
  engineOffer({
    engine: 'codex',
    capabilities: { ...engineOffer().capabilities, reasoningEffort: true },
  }),
]

const CODEX_MODELS: ModelOption[] = [{
  id: 'gpt-5-codex', label: 'GPT-5 Codex',
  reasoningEfforts: ['low', 'high'], defaultReasoningEffort: 'high',
}]

function dispatchBarProps(over: Partial<React.ComponentProps<typeof DispatchBar>> = {}) {
  return {
    repos: [githubRepo()],
    reposLoading: false,
    reposError: false,
    accounts: ACCOUNTS,
    models: [{ id: 'default', label: 'Default' }],
    engines: CLAUDE_ONLY,
    onDispatch: vi.fn().mockResolvedValue(undefined),
    onNavigate: vi.fn(),
    onOpenHighlighted: vi.fn(),
    onEnterList: vi.fn(),
    onJump: vi.fn(),
    onShowHelp: vi.fn(),
    onEscape: vi.fn(),
    isPending: false,
    ...over,
  }
}

describe('a11y: DispatchBar', () => {
  // `aria-controls` on the prompt points at the list column's listbox, which
  // lives in another region — stand it in so the audit is about the bar.
  function withListbox(ui: React.ReactElement) {
    return <div><div id="session-listbox" /><div id="dispatch-root">{ui}</div></div>
  }

  it('has no axe violations with a single engine offered', async () => {
    const { container } = render(withListbox(<DispatchBar {...dispatchBarProps()} />))
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })

  it('has no axe violations with the Agent selector and a runner picker', async () => {
    const { container } = render(withListbox(
      <DispatchBar {...dispatchBarProps({
        engines: BOTH_ENGINES,
        hosts: [{ name: 'edge' }],
      })} />,
    ))
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })

  it('has no axe violations with the reasoning-effort selector showing', async () => {
    const user = userEvent.setup()
    const { container } = render(withListbox(
      <DispatchBar {...dispatchBarProps({ engines: BOTH_ENGINES, models: CODEX_MODELS })} />,
    ))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Agent' }), 'codex')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Account' }), 'chatgpt')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Model' }), 'gpt-5-codex')
    expect(screen.getByRole('combobox', { name: /reasoning/i })).toBeInTheDocument()
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })
})

// ── AddAccountDialog ───────────────────────────────────────────────────────
//
// Three provider tabs and two device-code panels; the flows are driven by hooks,
// so those are the one thing stubbed here (the dialog owns them so they are only
// live while it is open).

// A PARTIAL mock: `SessionList` above renders through the real `@/api/hooks`,
// so replacing the module wholesale would break its audit rather than this one.
vi.mock('@/api/hooks', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/hooks')>(),
  useAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddAccountStatus: vi.fn(() => ({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false })),
  useCancelAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddOpenAIAccount: vi.fn(() => ({
    mutate: vi.fn((_name: string, options?: { onSuccess?: () => void }) => options?.onSuccess?.()),
    isPending: false,
  })),
  useOpenAIAddStatus: vi.fn((_name: string, enabled: boolean) => ({
    data: enabled ? {
      state: 'awaiting_authorization',
      userCode: 'WXYZ-9876',
      verificationUri: 'https://chatgpt.com/device',
    } : undefined,
    isLoading: false,
  })),
  useCancelOpenAIAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

describe('a11y: AddAccountDialog', () => {
  it('has no axe violations on the default (Anthropic) provider', async () => {
    const { baseElement } = render(<AddAccountDialog onClose={vi.fn()} />, { wrapper: qcWrapper })
    const results = await axe(baseElement)
    expect(results).toHaveNoViolations()
  })

  it('has no axe violations on the OpenAI device-code panel', async () => {
    const user = userEvent.setup()
    const { baseElement } = render(
      <AddAccountDialog onClose={vi.fn()} initialName="chatgpt" />, { wrapper: qcWrapper })
    await user.click(screen.getByRole('button', { name: /openai \(chatgpt\)/i }))
    await user.click(screen.getByRole('button', { name: /^add$/i }))
    expect(screen.getByText('WXYZ-9876')).toBeInTheDocument()
    const results = await axe(baseElement)
    expect(results).toHaveNoViolations()
  })
})
