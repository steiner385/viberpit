/**
 * Automated WCAG 2.1 AA accessibility audit for management surfaces.
 *
 * Uses jest-axe (axe-core) to catch labelling, role, focus, and contrast-attribute
 * violations. The xterm.js Terminal component is intentionally excluded — it carries
 * a documented partial-conformance note (see docs/ACCESSIBILITY.md).
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { AllLimitedBanner } from '../AllLimitedBanner'
import { EmptyState } from '../EmptyState'
import { SessionList } from '../SessionList'
import { UsageBadge, StatusPill } from '../UsageBadge'
import type { Session, Account } from '@/api/types'

expect.extend(toHaveNoViolations)

// ── test data ──────────────────────────────────────────────────────────────

const ACCOUNTS: Account[] = [
  {
    name: 'alice',
    status: 'ok',
    usage: { session: { pct: 30, resetsAt: null }, weekly: { pct: 50, resetsAt: null } },
  },
  {
    name: 'bob',
    status: 'limited',
    usage: { session: { pct: 95, resetsAt: null }, weekly: { pct: 80, resetsAt: null } },
  },
]

const SESSIONS: Session[] = [
  {
    id: 'sess-1',
    name: 'Work session',
    accountName: 'alice',
    status: 'in_progress',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  },
  {
    id: 'sess-2',
    name: 'Research',
    accountName: 'bob',
    status: 'idle',
    createdAt: '2026-06-19T11:00:00Z',
    updatedAt: '2026-06-19T11:00:00Z',
  },
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
    const { container } = render(<EmptyState onNew={vi.fn()} />)
    const results = await axe(container)
    expect(results).toHaveNoViolations()
  })
})

// ── SessionList ────────────────────────────────────────────────────────────

describe('a11y: SessionList', () => {
  const handlers = {
    onNew: vi.fn(),
    onOpen: vi.fn(),
    onRename: vi.fn(),
    onClose: vi.fn(),
    onDelete: vi.fn(),
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
