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
// (`@md:*`, resolved against the card's own width — the drag-resizable sidebar —
// not the viewport). This replaced viewport `lg:` breakpoints, which left a
// narrow sidebar on a wide window in the full desktop layout and overflowed the
// badge row under the action icons. When the card is narrow, only the
// at-a-glance essentials (title, account chip, status pill) and a single resume
// action survive; everything else is `hidden @md:*`. These assertions lock that
// contract so a refactor can't silently un-hide the demoted elements (or hide an
// essential) in a narrow card.
//
// The threshold must be REACHABLE inside the sidebar. Container queries resolve
// against the card's content box, and the card's width budget tops out at
// SIDEBAR_MAX 560 − sidebar p-2 (16) − scrollbar (~15) − card border (2) − card
// px-3 (24) ≈ 503px. `@lg` (32rem = 512px) is therefore unsatisfiable at ANY
// permitted sidebar width — gating on it hid the badges/telemetry/actions for
// every desktop user. `@md` (28rem = 448px) engages once the sidebar is dragged
// to ≳505px while keeping the compact layout at the 320px default.
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
    expect(screen.getByTestId('card-telemetry')).toHaveClass('hidden', '@md:flex')
  })

  it('gates the Goal badge to a wide card', () => {
    renderCard(rich)
    expect(screen.getByTestId('card-goal')).toHaveClass('hidden', '@md:inline-flex')
  })

  it('gates the PR badge to a wide card', () => {
    renderCard(rich)
    expect(screen.getByTestId('card-prs')).toHaveClass('hidden', '@md:inline-flex')
  })

  it('never gates content on a container width the sidebar cannot reach', () => {
    // Regression guard for the unreachable-breakpoint bug: `@lg:` (512px)
    // exceeds the card's ~503px max content width inside the sidebar, so any
    // element gated on it is permanently invisible on desktop. No card element
    // may use an `@lg:` (or larger) container variant.
    const { container } = renderCard(rich)
    const unreachable = container.querySelectorAll('[class*="@lg:"], [class*="@xl:"]')
    expect([...unreachable].map((el) => el.getAttribute('data-testid') ?? el.className)).toEqual([])
  })

  it('gates rename/archive/delete to desktop (lg+) but keeps resume and quick-replies always visible', () => {
    renderCard(rich)
    const secondary = screen.getByTestId('card-secondary-actions')
    // Hidden below lg via viewport breakpoint; dissolves to `contents` at lg+
    // so its children become direct flex members of the action row.
    expect(secondary).toHaveClass('hidden', 'lg:contents')
    expect(within(secondary).getByRole('button', { name: /rename session/i })).toBeInTheDocument()
    expect(within(secondary).getByRole('button', { name: /archive session/i })).toBeInTheDocument()
    expect(within(secondary).getByRole('button', { name: /delete session/i })).toBeInTheDocument()
    // Resume and Quick Replies are always visible — they must NOT live inside
    // the desktop-only group.
    const resume = screen.getByRole('button', { name: /resume session/i })
    expect(secondary).not.toContainElement(resume)
    expect(resume).not.toHaveClass('hidden')
    const quickReplies = screen.getByRole('button', { name: /quick replies/i })
    expect(secondary).not.toContainElement(quickReplies)
  })

  it('keeps the essentials (title + account chip) visible when narrow', () => {
    renderCard(rich)
    const identity = screen.getByTestId('card-identity')
    expect(within(identity).getByText('Fix login bug')).not.toHaveClass('hidden')
    expect(within(identity).getByText('work')).not.toHaveClass('hidden')
  })
})

// Badge-footer contract (session-card overlap fix).
// The secondary badges used to live in a single non-wrapping, non-shrinking
// cluster inside the context row. With a badge-rich session (goal + autopilot +
// provenance + PRs) that cluster was WIDER than the row, so it overflowed the
// card border and painted underneath the hover action buttons — measured
// 25–32px of real overlap at a wide (≥505px) sidebar. The fix moves the badges
// into a dedicated footer row that (a) wraps, so any badge count stays inside
// the card, and (b) is a sibling of the open-session button, so its interactive
// chips (PR popover, provenance link, Fix-PR) are no longer nested-interactive
// violations. These tests lock both properties.
describe('SessionCard badge footer', () => {
  const rich: Session = {
    ...base,
    activeGoal: 'ship the thing',
    autoArchiveOptOut: true,
    assistedBy: 'autopilot',
    prs: [
      { number: 1, url: 'u1', state: 'open', checks: 'passing', title: 'A', deployed: true },
      { number: 2, url: 'u2', state: 'open', checks: 'passing', title: 'B', deployed: false },
    ],
  }

  it('collects the secondary badges into a wrapping footer row', () => {
    renderCard(rich)
    const footer = screen.getByTestId('card-badges')
    // `flex-wrap` is the overflow-safety property: N badges lay out inside the
    // card at any width instead of exceeding the row and colliding with the
    // action buttons. Wide-card-only, like the badges it hosts.
    expect(footer).toHaveClass('flex-wrap', 'hidden', '@md:flex')
    expect(footer).toContainElement(screen.getByTestId('card-goal'))
    expect(footer).toContainElement(screen.getByTestId('card-no-autoarchive-badge'))
    expect(footer).toContainElement(screen.getByTestId('card-provenance'))
    expect(footer).toContainElement(screen.getByTestId('card-prs'))
  })

  it('renders no footer row when the session has no badges', () => {
    renderCard(base)
    // An empty flex row still costs gap/margin height — it must not mount.
    expect(screen.queryByTestId('card-badges')).not.toBeInTheDocument()
  })

  it('keeps interactive elements out of the open-session button (nested-interactive)', () => {
    renderCard(rich)
    const open = screen.getByRole('button', { name: /open session/i })
    // Nested <button>/<a> inside the open button is invalid HTML and an axe
    // nested-interactive violation; it also made badge clicks fight the
    // open-session click. The button may contain text and passive spans only.
    expect(open.querySelectorAll('button, a, [tabindex]')).toHaveLength(0)
  })

  it('keeps the status pill pinned in the context row at every width', () => {
    renderCard(rich)
    const context = screen.getByTestId('card-context')
    // The pill is the context row's ONLY chip — placement can't vary with badge
    // count anymore (badges live in the footer). Not gated: visible when narrow.
    expect(within(context).getByLabelText(/^Status:/)).toBeInTheDocument()
    expect(context).not.toContainElement(screen.getByTestId('card-prs'))
    expect(context).not.toContainElement(screen.getByTestId('card-goal'))
  })
})
