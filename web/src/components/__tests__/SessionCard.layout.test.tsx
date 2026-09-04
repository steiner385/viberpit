import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import type { ComponentProps } from 'react'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'
import { pullRequest, session } from '@/test/factories'

// Dense two-line card contract. The card lives in a drag-resizable sidebar, so
// its layout must be deterministic at every width and for every session shape:
// exactly two fixed rows — line 1 (identity: status dot, title, marker cluster,
// PRs, usage, account, actions) and line 2 (context narrative: repo · host ·
// status phrase · last-active) — plus a colored status rail on the left edge.
// Every optional element has ONE fixed slot; nothing floats between rows based
// on content. These tests lock that skeleton so a future tweak can't regress to
// the old grab-bag badge footer / floating status pill.

const base: Session = session({
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  // Non-busy: the "active … ago" label is suppressed on the busy tier (see
  // SessionCard.meta.test), and these skeleton tests assert its slot.
  status: 'done',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
})

const noop = () => {}

function renderCard(session: Session, extra: Partial<ComponentProps<typeof SessionCard>> = {}) {
  return render(
    <MemoryRouter>
      <SessionCard
        session={session}
        account={undefined}
        onOpen={noop}
        onRename={noop}
        onClose={noop}
        onDelete={noop}
        onQuickResume={noop}
        {...extra}
      />
    </MemoryRouter>,
  )
}

const rich: Session = {
  ...base,
  repo: 'cairnea/KinDash',
  host: 'dobby',
  activeGoal: 'ship the thing',
  // `string | null` — the sticky opt-out reason, not a boolean flag. The card
  // only tests it for truthiness (SessionCard.tsx:522), so this is the same case.
  autoArchiveOptOut: 'manual-unarchive',
  assistedBy: 'autopilot',
  latestSuggestions: ['run the tests', 'open the PR'],
  prs: [
    pullRequest({ number: 1, url: 'u1', checks: 'passing', title: 'A', deployed: true }),
    pullRequest({ number: 2, url: 'u2', checks: 'passing', title: 'B', deployed: false }),
  ],
}

describe('SessionCard two-line skeleton', () => {
  it('renders exactly the two fixed rows — no telemetry row, no badge footer', () => {
    renderCard(rich)
    expect(screen.getByTestId('card-identity')).toBeInTheDocument()
    expect(screen.getByTestId('card-context')).toBeInTheDocument()
    expect(screen.queryByTestId('card-telemetry')).not.toBeInTheDocument()
    expect(screen.queryByTestId('card-badges')).not.toBeInTheDocument()
  })

  it('truncates the title inside the open button so the right cluster stays pinned', () => {
    renderCard(base)
    const open = screen.getByRole('button', { name: /open session/i })
    const title = within(open).getByText('Fix login bug')
    expect(title).toHaveClass('truncate')
    expect(open).toHaveClass('flex-1', 'min-w-0')
  })

  it('caps and truncates the account chip so a long email cannot starve the title', () => {
    renderCard({ ...base, accountName: 'tonystein85@gmail.com' })
    const identity = screen.getByTestId('card-identity')
    const chip = within(identity).getByText('tonystein85@gmail.com')
    expect(chip).toHaveClass('truncate', 'max-w-[30%]')
  })

  it('line 2 carries repo, host, status phrase and last-active — in the context row', () => {
    renderCard(rich)
    const context = screen.getByTestId('card-context')
    expect(within(context).getByText('cairnea/KinDash')).toBeInTheDocument()
    expect(within(context).getByTestId('card-host')).toHaveTextContent('dobby')
    expect(within(context).getByLabelText(/^Status:/)).toBeInTheDocument()
    expect(within(context).getByText(/^active \d+[smhd] ago$/)).toBeInTheDocument()
  })

  it('the status phrase carries the presentation label and its tier text color', () => {
    renderCard({ ...base, status: 'waiting_input' })
    const phrase = screen.getByLabelText('Status: Permission needed')
    expect(phrase.className).toMatch(/warning/)
  })

  it('marker cluster, PRs and usage live in line 1, not line 2', () => {
    renderCard(rich, {
      account: { name: 'work', status: 'ok', nickname: null, usage: { session: { pct: 74, resetsAt: null } } } as never,
    })
    const identity = screen.getByTestId('card-identity')
    expect(identity).toContainElement(screen.getByTestId('card-goal'))
    expect(identity).toContainElement(screen.getByTestId('card-provenance'))
    expect(identity).toContainElement(screen.getByTestId('card-prs'))
    expect(identity).toContainElement(screen.getByTestId('card-usage'))
    const context = screen.getByTestId('card-context')
    expect(context).not.toContainElement(screen.getByTestId('card-goal'))
    expect(context).not.toContainElement(screen.getByTestId('card-prs'))
  })
})

describe('SessionCard status rail', () => {
  it.each([
    ['in_progress', /success/],
    ['waiting_input', /warning/],
    ['needs_attention', /danger/],
    ['done', /neutral/],
  ] as const)('colors the rail by tier for %s', (status, color) => {
    renderCard({ ...base, status })
    expect(screen.getByTestId('card-status-rail').className).toMatch(color)
  })

  it('pulses the status dot only on action-required tiers', () => {
    renderCard({ ...base, status: 'waiting_input' })
    expect(screen.getByTestId('card-status-dot').className).toMatch(/animate-pulse/)
  })

  it('does not pulse the dot on calm tiers', () => {
    renderCard(base)
    expect(screen.getByTestId('card-status-dot').className).not.toMatch(/animate-pulse/)
  })
})

// Narrow-card declutter: same policy as before, new slots. Below @md (container
// query against the card's own width) only the essentials survive: dot, title,
// account chip, status phrase, actions. Markers, PR pills, usage and the
// timestamp are wide-card only.
describe('SessionCard narrow-card declutter contract', () => {
  it('gates the marker cluster, PRs and usage to a wide card', () => {
    renderCard(rich, {
      account: { name: 'work', status: 'ok', nickname: null, usage: { session: { pct: 74, resetsAt: null } } } as never,
    })
    expect(screen.getByTestId('card-goal')).toHaveClass('hidden', '@md:inline-flex')
    expect(screen.getByTestId('card-prs')).toHaveClass('hidden', '@md:inline-flex')
    expect(screen.getByTestId('card-usage')).toHaveClass('hidden', '@md:inline-flex')
  })

  it('never gates content on a container width the sidebar cannot reach', () => {
    const { container } = renderCard(rich)
    const unreachable = container.querySelectorAll('[class*="@lg:"], [class*="@xl:"]')
    expect([...unreachable].map((el) => el.getAttribute('data-testid') ?? el.className)).toEqual([])
  })

  it('keeps the essentials (title, account chip, status phrase) ungated', () => {
    renderCard(rich)
    expect(within(screen.getByTestId('card-identity')).getByText('Fix login bug')).not.toHaveClass('hidden')
    expect(within(screen.getByTestId('card-identity')).getByText('work')).not.toHaveClass('hidden')
    expect(screen.getByLabelText(/^Status:/)).not.toHaveClass('hidden')
  })
})

// Unified actions contract: Resume (✦) and the ⋯ menu are the ONLY action
// controls, permanently visible on every device — no hover-revealed icon row,
// no desktop/mobile split, no separate quick-replies dropdown.
describe('SessionCard unified actions', () => {
  it('shows Resume and More-actions as the only inline action buttons', () => {
    renderCard(rich)
    expect(screen.getByRole('button', { name: /resume session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /more actions for session/i })).toBeInTheDocument()
    // The old split is gone: no desktop-only group, no standalone quick-replies
    // trigger, no always-visible rename/archive/delete icons.
    expect(screen.queryByTestId('card-secondary-actions')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /quick replies for/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^rename session/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^archive session/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^delete session/i })).not.toBeInTheDocument()
  })

  it('never hides the action cluster behind hover', () => {
    renderCard(rich)
    const more = screen.getByRole('button', { name: /more actions for session/i })
    // The old cluster used opacity-0 + group-hover reveal; the new one is static.
    expect(more.closest('[class*="opacity-0"]')).toBeNull()
  })

  it('the ⋯ button stays available while the session is busy (management actions must not vanish)', () => {
    renderCard({ ...base, status: 'in_progress' })
    expect(screen.getByRole('button', { name: /more actions for session/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /resume session/i })).not.toBeInTheDocument()
  })
})

describe('SessionCard interaction surface', () => {
  it('keeps interactive elements out of the open-session button (nested-interactive)', () => {
    renderCard(rich, { prAutofixMode: 'propose' })
    const open = screen.getByRole('button', { name: /open session/i })
    expect(open.querySelectorAll('button, a, [tabindex]')).toHaveLength(0)
  })

  it('clicking the context row (empty card area) opens the session', () => {
    const onOpen = vi.fn()
    renderCard(rich, { onOpen })
    fireEvent.click(screen.getByTestId('card-context'))
    expect(onOpen).toHaveBeenCalledWith('1')
  })

  it('clicking an interactive chip does NOT also open the session', () => {
    const onOpen = vi.fn()
    renderCard(rich, { onOpen })
    fireEvent.click(screen.getByTestId('card-provenance'))
    expect(onOpen).not.toHaveBeenCalled()
  })
})
