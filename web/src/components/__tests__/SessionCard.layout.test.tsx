import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

// The session card lives in a drag-resizable sidebar, so its layout must be
// deterministic at every width: the same three rows (identity / context /
// telemetry) in the same order on every card, each row truncating rather than
// wrapping. These tests lock that skeleton in place so a future tweak can't
// regress back to content-dependent flex-wrap (where the account chip and the
// usage bar landed in a different spot on every card).

const base: Session = {
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  // Non-busy: the telemetry row's "active … ago" label is suppressed on the busy
  // tier (see SessionCard.meta.test), so the skeleton tests below — which assert
  // the telemetry row and its timestamp — use a handed-back status.
  status: 'done',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
}

const noop = () => {}

function renderCard(session: Session) {
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
      />
    </MemoryRouter>,
  )
}

describe('SessionCard deterministic skeleton', () => {
  it('renders the three fixed rows in order', () => {
    renderCard(base)
    expect(screen.getByTestId('card-identity')).toBeInTheDocument()
    expect(screen.getByTestId('card-context')).toBeInTheDocument()
    expect(screen.getByTestId('card-telemetry')).toBeInTheDocument()
  })

  it('truncates the title so the account chip stays pinned on the identity row', () => {
    renderCard(base)
    const identity = screen.getByTestId('card-identity')
    const title = within(identity).getByText('Fix login bug')
    // Without `flex-1 min-w-0`, `truncate` is a no-op and a long title shoves
    // the account chip onto a second line — the original inconsistency.
    expect(title).toHaveClass('truncate', 'min-w-0', 'flex-1')
    expect(within(identity).getByText('work')).toBeInTheDocument()
  })

  it('caps and truncates the account chip so a long email cannot starve the title', () => {
    // On a narrow (mobile) viewport a long account email used to be pinned at
    // full width (`flex-shrink-0`, no cap), collapsing the session name to a few
    // characters ("M…"). The chip must yield instead: cap its share and truncate
    // it so the title — the primary identifier — keeps the bulk of the row.
    renderCard({ ...base, accountName: 'tonystein85@gmail.com' })
    const identity = screen.getByTestId('card-identity')
    const chip = within(identity).getByText('tonystein85@gmail.com')
    expect(chip).toHaveClass('truncate', 'max-w-[45%]')
  })

  it('places the last-active timestamp in the telemetry row, not the context row', () => {
    renderCard(base)
    const telemetry = screen.getByTestId('card-telemetry')
    expect(within(telemetry).getByText(/^active \d+[smhd] ago$/)).toBeInTheDocument()
  })
})

// Mobile declutter contract (PR: mobile session-card declutter).
// On a phone the card crammed title + account + repo + status + Goal + PRs +
// usage% + "active … ago" + five 44px action icons into ~360px. Per-element
// truncation can shrink things but can't *drop* one when space runs out, so the
// secondary signals are gated to a wide card via Tailwind container queries
// (`@lg:*`, resolved against the card's own width — the drag-resizable sidebar —
// not the viewport). This replaced viewport `lg:` breakpoints, which left a
// narrow sidebar on a wide window in the full desktop layout and overflowed the
// badge row under the action icons. When the card is narrow, only the
// at-a-glance essentials (title, account chip, status pill) and a single resume
// action survive; everything else is `hidden @lg:*`. These assertions lock that
// contract so a refactor can't silently un-hide the demoted elements (or hide an
// essential) in a narrow card.
describe('SessionCard narrow-card declutter contract', () => {
  const rich: Session = {
    ...base,
    activeGoal: 'ship the thing',
    latestSuggestions: ['run the tests', 'open the PR'],
    prs: [
      { number: 1, url: 'u1', state: 'open', checks: 'passing', title: 'A', deployed: true },
      { number: 2, url: 'u2', state: 'open', checks: 'passing', title: 'B', deployed: false },
    ],
  }

  it('gates the telemetry row to a wide card (hidden when narrow)', () => {
    renderCard(rich)
    expect(screen.getByTestId('card-telemetry')).toHaveClass('hidden', '@lg:flex')
  })

  it('gates the Goal badge to a wide card', () => {
    renderCard(rich)
    expect(screen.getByTestId('card-goal')).toHaveClass('hidden', '@lg:inline-flex')
  })

  it('gates the PR badge to a wide card', () => {
    renderCard(rich)
    expect(screen.getByTestId('card-prs')).toHaveClass('hidden', '@lg:inline-flex')
  })

  it('gates rename/archive/delete to a wide card but keeps resume when narrow', () => {
    renderCard(rich)
    const secondary = screen.getByTestId('card-secondary-actions')
    expect(secondary).toHaveClass('hidden', '@lg:contents')
    expect(within(secondary).getByRole('button', { name: /rename session/i })).toBeInTheDocument()
    expect(within(secondary).getByRole('button', { name: /archive session/i })).toBeInTheDocument()
    expect(within(secondary).getByRole('button', { name: /delete session/i })).toBeInTheDocument()
    // Resume is the one action kept inline when narrow — it must NOT live in the
    // wide-card-only group.
    const resume = screen.getByRole('button', { name: /resume session/i })
    expect(secondary).not.toContainElement(resume)
    expect(resume).not.toHaveClass('hidden')
  })

  it('keeps the essentials (title + account chip) visible when narrow', () => {
    renderCard(rich)
    const identity = screen.getByTestId('card-identity')
    expect(within(identity).getByText('Fix login bug')).not.toHaveClass('hidden')
    expect(within(identity).getByText('work')).not.toHaveClass('hidden')
  })
})
