/**
 * SessionHeader — the open session's top bar, mounted directly.
 *
 * `MainPaneSession`'s own suites keep their Back / chevron / actions cases and
 * still pass unchanged (the pane renders this header), so nothing was removed
 * from them and R9 has nothing to map for those. What is here is the CONTRACT of
 * the extracted component, asserted without the pane, plus the three things Task
 * 10 adds: the region marker, click-to-rename, and the status pill.
 *
 * Scope note on axe: jsdom applies no stylesheet, so axe here covers roles,
 * names and ARIA wiring ONLY. Its `color-contrast` rule always returns
 * `incomplete` and `toHaveNoViolations()` inspects `violations` alone — a green
 * assertion below is NOT evidence of adequate contrast. Contrast is checked
 * separately by src/__tests__/contrast.test.ts.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { QK } from '@/api/hooks'
import type { AgentCapabilities, EngineOffer, Session, SessionStatus } from '@/api/types'
import { session as makeSession } from '@/test/factories'
import { SessionHeader, type SessionHeaderProps } from './SessionHeader'

expect.extend(toHaveNoViolations)

// ── typed factories (R10: real values, never `as unknown as T`) ───────────────
// Shape lives in @/test/factories; this suite only adds `updatedAt` (the header
// renders a last-activity time, so its fixtures need recorded activity).

const session = (over: Partial<Session> = {}): Session =>
  makeSession({ updatedAt: '2026-06-19T10:00:00Z', ...over })

interface RenderOpts {
  props?: Partial<SessionHeaderProps>
  sessions?: Session[]
  /** Seed `/api/config`'s engine report; omit for "the server has not answered". */
  engines?: EngineOffer[]
}

function renderHeader({ props = {}, sessions = [session()], engines }: RenderOpts = {}) {
  const onBack = vi.fn()
  const onOpenSession = vi.fn()
  const onClosed = vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(QK.sessions, sessions)
  if (engines) qc.setQueryData(QK.config, { repoRoot: '/repo', engines })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const utils = render(
    <SessionHeader
      sessionId="sess-1"
      title="My Session"
      goal={null}
      goalSource={null}
      status="idle"
      onBack={onBack}
      onOpenSession={onOpenSession}
      onClosed={onClosed}
      {...props}
    />,
    { wrapper },
  )
  return { ...utils, onBack, onOpenSession, onClosed }
}

const three = () => [session({ id: 'a', name: 'A' }), session({ id: 'sess-1' }), session({ id: 'c', name: 'C' })]

beforeEach(() => localStorage.clear())

// ── the region marker (DEC-1) ────────────────────────────────────────────────

describe('SessionHeader — data-region', () => {
  it('carries data-region="session.header", exactly once', () => {
    renderHeader()
    expect(document.querySelectorAll('[data-region="session.header"]')).toHaveLength(1)
  })

  it('marks the bar itself — Back, the title and the actions are all inside it', async () => {
    // A marker on an empty wrapper beside the row would satisfy the count above
    // and name nothing. `[data-region]` has to be the node the region IS.
    renderHeader()
    const region = document.querySelector('[data-region="session.header"]')
    expect(region).not.toBeNull()
    expect(region).toContainElement(screen.getByRole('button', { name: 'Back to session list' }))
    expect(region).toContainElement(screen.getByRole('button', { name: 'Rename session My Session' }))
    expect(region).toContainElement(screen.getByRole('button', { name: 'More session actions' }))
  })

  it('claims no app.* region — those are the shell\'s, and its matrix answers for them', () => {
    renderHeader()
    const ids = [...document.querySelectorAll('[data-region]')]
      .map((el) => el.getAttribute('data-region'))
    expect(ids.filter((id) => id?.startsWith('app.'))).toEqual([])
  })
})

// ── Back and the chevrons (unchanged behaviour, asserted on the extracted unit) ─

describe('SessionHeader — Back and the prev/next chevrons', () => {
  it('Back calls onBack', async () => {
    const { onBack } = renderHeader()
    await userEvent.click(screen.getByRole('button', { name: 'Back to session list' }))
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('renders the chevrons with 44px touch targets when neighbours exist', () => {
    renderHeader({ sessions: three() })
    const next = screen.getByRole('button', { name: 'Next session' })
    expect(screen.getByRole('button', { name: 'Previous session' })).toBeInTheDocument()
    expect(next.className).toMatch(/min-h-target/)
    expect(next.className).toMatch(/min-w-target/)
  })

  it('› opens the next session and ‹ the previous one', async () => {
    const { onOpenSession } = renderHeader({ sessions: three() })
    await userEvent.click(screen.getByRole('button', { name: 'Next session' }))
    expect(onOpenSession).toHaveBeenCalledWith('c')
    await userEvent.click(screen.getByRole('button', { name: 'Previous session' }))
    expect(onOpenSession).toHaveBeenCalledWith('a')
  })

  it('hides the chevrons when there is only one session', () => {
    renderHeader()
    expect(screen.queryByRole('button', { name: 'Next session' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Previous session' })).toBeNull()
  })
})

// ── click-to-rename ──────────────────────────────────────────────────────────

describe('SessionHeader — click-to-rename', () => {
  it('the title is a button whose accessible name includes the session name', () => {
    renderHeader({ props: { title: 'Refactor the ledger' } })
    const title = screen.getByRole('button', { name: 'Rename session Refactor the ledger' })
    expect(title).toHaveTextContent('Refactor the ledger')
    // A <div onClick> would render the same text and read the same on screen —
    // this is the half that would not survive it.
    expect(title.tagName).toBe('BUTTON')
    expect(title).toHaveAttribute('type', 'button')
  })

  it('clicking the title opens the rename dialog, seeded with the current name', async () => {
    renderHeader()
    expect(screen.queryByRole('dialog', { name: /rename session/i })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Rename session My Session' }))
    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
    expect(screen.getByLabelText('Session name')).toHaveValue('My Session')
  })

  it.each(['{Enter}', ' '])('%s on the focused title opens rename', async (key) => {
    renderHeader()
    screen.getByRole('button', { name: 'Rename session My Session' }).focus()
    await userEvent.keyboard(key)
    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
  })

  it('the ⋯ menu item opens the same dialog the title does — two triggers', async () => {
    // This case proves BOTH triggers reach a rename dialog, and that exactly one
    // is ever mounted. It does not, on its own, prove there is only one
    // implementation behind them — two independent flows would also show one
    // dialog at a time. The guard for that is in `SessionActions.test.tsx`
    // ("calls onRename, closes the menu, and opens no dialog of its own"): the
    // menu item delegates, so the only rename flow in the tree is this one.
    renderHeader()
    await userEvent.click(screen.getByRole('button', { name: 'More session actions' }))
    await userEvent.click(screen.getByRole('menuitem', { name: /rename/i }))
    expect(screen.getAllByRole('dialog', { name: /rename session/i })).toHaveLength(1)

    await userEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(screen.queryByRole('dialog', { name: /rename session/i })).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Rename session My Session' }))
    expect(screen.getAllByRole('dialog', { name: /rename session/i })).toHaveLength(1)
  })
})

// ── the status pill ──────────────────────────────────────────────────────────

describe('SessionHeader — the status pill', () => {
  // One row per TIER, not per status: the pill's job is to render the tier the
  // shared `session/statusTier` table assigns, so a status moved between tiers
  // there has to move here too.
  const CASES: [SessionStatus, string][] = [
    ['idle', 'Status: Ready'],                    // your-turn
    ['in_progress', 'Status: Working…'],          // busy
    ['waiting_input', 'Status: Permission needed'], // needs-you
    ['waiting_external', 'Status: Waiting…'],     // waiting
    ['needs_attention', 'Status: Turn failed'],   // error
  ]

  it.each(CASES)('%s renders the %s pill', (status, label) => {
    renderHeader({ props: { status } })
    expect(screen.getByLabelText(label)).toBeInTheDocument()
  })

  it('renders exactly one pill', () => {
    renderHeader()
    expect(screen.getAllByLabelText(/^Status: /)).toHaveLength(1)
  })
})

// ── the goal ─────────────────────────────────────────────────────────────────

describe('SessionHeader — the goal', () => {
  it('renders a distinct goal beside the title and hides one equal to it', () => {
    const { unmount } = renderHeader({ props: { goal: 'ship the header', goalSource: 'explicit' } })
    expect(screen.getByTestId('session-goal')).toHaveTextContent('ship the header')
    unmount()

    renderHeader({ props: { goal: 'My Session', goalSource: 'initial_prompt' } })
    expect(screen.queryByTestId('session-goal')).toBeNull()
  })
})

// ── session identity (Task 5.3) ──────────────────────────────────────────────
//
// The header names WHAT is running the session: agent (engine), account alias,
// runner host, model and reasoning effort. The record is read from the sessions
// query by this header's own `sessionId` — the same query the prev/next
// chevrons already walk — rather than from a global.

describe('SessionHeader — session identity', () => {
  const codex = () => [session({
    id: 'sess-1', engine: 'codex', accountName: 'chatgpt-pro', host: 'rocky',
    model: 'gpt-5.1-codex', reasoningEffort: 'high',
  })]

  it('names the agent, account, runner, model and effort of a Codex session', () => {
    renderHeader({ sessions: codex() })
    const identity = screen.getByTestId('session-identity')
    expect(identity).toHaveTextContent('Codex')
    expect(identity).toHaveTextContent('chatgpt-pro')
    expect(identity).toHaveTextContent('rocky')
    expect(identity).toHaveTextContent('GPT-5.1 Codex')
    expect(identity).toHaveTextContent('high')
  })

  it('omits the agent chip for the default Claude Code engine', () => {
    renderHeader({ sessions: [session({ id: 'sess-1', model: 'opus' })] })
    const identity = screen.getByTestId('session-identity')
    expect(identity).not.toHaveTextContent('Claude Code')
    expect(identity).toHaveTextContent('Opus')
  })

  it('omits the effort and host readouts when the record carries none', () => {
    renderHeader({ sessions: [session({ id: 'sess-1' })] })
    expect(screen.queryByLabelText('Reasoning effort')).toBeNull()
    expect(screen.queryByLabelText('Runner host')).toBeNull()
  })

  it('renders nothing when the record is not in the list yet', () => {
    renderHeader({ sessions: [session({ id: 'other' })] })
    expect(screen.queryByTestId('session-identity')).toBeNull()
  })

  it('truncates a long account alias and model instead of overflowing the bar', () => {
    renderHeader({ sessions: [session({
      id: 'sess-1',
      accountName: 'a-really-long-account-alias@some-organisation.example.com',
      model: 'claude-opus-4-8-with-an-absurdly-long-suffix',
    })] })
    for (const label of ['Account', 'Model']) {
      expect(screen.getByLabelText(label), label).toHaveClass('truncate')
    }
  })
})

// ── accessibility ────────────────────────────────────────────────────────────

describe('SessionHeader — accessibility', () => {
  it('has no axe violations with the chevrons and a goal on screen', async () => {
    const { container } = renderHeader({
      sessions: three(),
      props: { goal: 'ship the header', goalSource: 'explicit', status: 'waiting_input' },
    })
    expect(await axe(container)).toHaveNoViolations()
  })

  it('has no axe violations with the full identity cluster on screen', async () => {
    const { container } = renderHeader({
      sessions: [session({
        id: 'sess-1', engine: 'codex', host: 'rocky',
        model: 'gpt-5.1-codex', reasoningEffort: 'high',
      })],
    })
    expect(await axe(container)).toHaveNoViolations()
  })
})


// ── capability-gated actions ─────────────────────────────────────────────────

/** Every capability off but the one under test, so a gate cannot pass by luck. */
const caps = (over: Partial<AgentCapabilities> = {}): AgentCapabilities => ({
  models: false, reasoningEffort: false, steer: false, interrupt: false,
  approvals: false, questions: false, fork: false, adopt: false,
  selfModelSwitch: false, subagents: false, attachments: false,
  accountRotation: false, ...over,
})

const offer = (engine: 'claude_code' | 'codex', capabilities: AgentCapabilities): EngineOffer => ({
  engine, available: true, hosts: [], unavailableReasons: {}, blockedReason: null, capabilities,
})

describe('SessionHeader — account rotation is capability-gated', () => {
  async function openActions() {
    await userEvent.click(screen.getByRole('button', { name: 'More session actions' }))
  }

  it('offers "next best account" for an engine that declares accountRotation', async () => {
    renderHeader({
      props: { onRotate: vi.fn() },
      engines: [offer('claude_code', caps({ accountRotation: true }))],
    })
    await openActions()
    expect(screen.getByRole('menuitem', { name: /next best account/i })).toBeInTheDocument()
  })

  it('hides it for an engine that does not', async () => {
    // Rotation recomposes the target account's config dir and syncs credentials
    // back; the server REFUSES an engine without the capability, so offering the
    // action would offer a guaranteed error.
    renderHeader({
      props: { onRotate: vi.fn() },
      sessions: [session({ id: 'sess-1', engine: 'codex' })],
      engines: [offer('codex', caps({ accountRotation: false }))],
    })
    await openActions()
    expect(screen.queryByRole('menuitem', { name: /next best account/i })).toBeNull()
  })

  it('keeps offering it while /api/config has not answered', async () => {
    // Unknown counts as present: the report is advisory (the server refuses
    // regardless), so a slow or old config must not hide a working control.
    renderHeader({ props: { onRotate: vi.fn() } })
    await openActions()
    expect(screen.getByRole('menuitem', { name: /next best account/i })).toBeInTheDocument()
  })
})
