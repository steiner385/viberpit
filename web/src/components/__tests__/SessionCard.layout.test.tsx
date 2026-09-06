import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import type { ComponentProps } from 'react'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'
import { account as accountFixture, anthropicUsage, pullRequest, session } from '@/test/factories'

// Dense three-line card contract. The card lives in a drag-resizable sidebar, so
// its layout must be deterministic at every width and for every session shape:
// exactly three fixed rows — line 1 (identity: status dot, title, actions), line
// 2 (context narrative: repo · host · status phrase · last-active) and line 3
// (meta: marker cluster, PRs, usage, model, account) — plus a colored status
// rail on the left edge.
//
// Line 1 exists to carry the TITLE. It was a single flex row where every child
// was flex-shrink-0 except the title, so the title absorbed 100% of the width
// deficit and rendered as "I…" on a ~500px card. The fix is structural: nothing
// that can grow shares the title's row. The guard below is what keeps it that
// way — a chip added back to line 1 fails it.
//
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
  model: 'opus',
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

// Everything the `rich` fixture puts on the meta row. (`card-engine` /
// `card-effort` are Codex-only and covered in SessionCard.model.test.)
const META_CHIPS = [
  'card-goal', 'card-provenance', 'card-prs', 'card-usage',
  'card-model', 'card-account',
] as const

// The only testid'd elements allowed on line 1. An allowlist rather than a list
// of known chips: the bug was a row that accumulated growable children, so the
// guard has to fail for a chip nobody has written yet.
const LINE_1_ALLOWED = new Set(['card-status-dot', 'card-autopilot-active'])

describe('SessionCard three-line skeleton', () => {
  it('renders exactly the three fixed rows — no telemetry row, no badge footer', () => {
    renderCard(rich)
    expect(screen.getByTestId('card-identity')).toBeInTheDocument()
    expect(screen.getByTestId('card-context')).toBeInTheDocument()
    expect(screen.getByTestId('card-meta')).toBeInTheDocument()
    expect(screen.queryByTestId('card-telemetry')).not.toBeInTheDocument()
    expect(screen.queryByTestId('card-badges')).not.toBeInTheDocument()
  })

  it('the meta row renders even when the session carries no chips but the account', () => {
    // A fixed slot, not a content-dependent one: a row that sometimes exists is
    // the vertical-jitter bug the "ONE fixed slot" rule above guards against.
    renderCard(base)
    const meta = screen.getByTestId('card-meta')
    expect(within(meta).getByTestId('card-account')).toBeInTheDocument()
    expect(within(meta).queryByTestId('card-model')).not.toBeInTheDocument()
  })

  it('truncates the title inside the open button so the right cluster stays pinned', () => {
    renderCard(base)
    const open = screen.getByRole('button', { name: /open session/i })
    const title = within(open).getByText('Fix login bug')
    expect(title).toHaveClass('truncate')
    expect(open).toHaveClass('flex-1', 'min-w-0')
  })

  it('caps and truncates the account chip on the meta row', () => {
    // The old 30% cap existed only to stop a long email starving the title. On
    // its own row there is no title to protect, and 30% clipped an ordinary
    // gmail address — so the cap widens but stays, for genuinely long values.
    renderCard({ ...base, accountName: 'tonystein85@gmail.com' })
    const meta = screen.getByTestId('card-meta')
    const chip = within(meta).getByText('tonystein85@gmail.com')
    expect(chip).toHaveClass('truncate', 'max-w-[50%]')
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

  it('marker cluster, PRs, usage, model and account live in the meta row', () => {
    renderCard(rich, {
      account: accountFixture({ name: 'work', nickname: null, usage: anthropicUsage(74, 20) }),
    })
    const meta = screen.getByTestId('card-meta')
    const context = screen.getByTestId('card-context')
    for (const id of META_CHIPS) {
      expect(meta, id).toContainElement(screen.getByTestId(id))
      expect(context, id).not.toContainElement(screen.getByTestId(id))
    }
  })

  // The mirror of the guard below: line 3 must not repeat line 1's bug in
  // reverse. Every child fixed-width means a full chip set overflows the card
  // instead of yielding, so exactly one child absorbs the deficit.
  it('the account chip is the meta row’s one yielding child', () => {
    renderCard(rich, {
      account: accountFixture({ name: 'work', nickname: null, usage: anthropicUsage(74, 20) }),
      prAutofixMode: 'propose',
    })
    const meta = screen.getByTestId('card-meta')
    const account = screen.getByTestId('card-account')
    expect(account).toHaveClass('min-w-0', 'truncate')
    expect(account).not.toHaveClass('flex-shrink-0')
    for (const child of meta.children) {
      if (child === account) continue
      expect(child.className, child.getAttribute('data-testid') ?? '').toMatch(/flex-shrink-0/)
    }
    // Clipping is NOT the alternative: PrList opens an `absolute z-20` popover
    // out of this row, so overflow-hidden here would cut it off.
    expect(meta.className).not.toMatch(/overflow-hidden/)
  })

  // THE regression guard for the barely-visible-title bug. Line 1 holds the
  // status dot, the autopilot marker, the title and the two action buttons —
  // all fixed-width but the title. Any chip added back here re-starves it.
  it('line 1 carries nothing that competes with the title for width', () => {
    renderCard(rich, {
      account: accountFixture({ name: 'work', nickname: null, usage: anthropicUsage(74, 20) }),
      prAutofixMode: 'propose',
    })
    const identity = screen.getByTestId('card-identity')
    const strays = [...identity.querySelectorAll('[data-testid]')]
      .map((el) => el.getAttribute('data-testid')!)
      .filter((id) => !LINE_1_ALLOWED.has(id))
    expect(strays).toEqual([])
    // Only the open control flexes; every other line-1 child is fixed-width, so
    // the title receives the row's whole remainder instead of its deficit.
    const open = screen.getByRole('button', { name: /open session/i })
    expect(open).toHaveClass('flex-1', 'min-w-0')
    for (const child of identity.children) {
      if (child === open) continue
      expect(child.className, child.outerHTML.slice(0, 120)).toMatch(/flex-shrink-0/)
    }
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
// account chip, status phrase, actions. Markers, PR pills, usage, the model
// chip and the timestamp are wide-card only.
describe('SessionCard narrow-card declutter contract', () => {
  it('gates the marker cluster, PRs, usage and model to a wide card', () => {
    renderCard(rich, {
      account: accountFixture({ name: 'work', nickname: null, usage: anthropicUsage(74, 20) }),
    })
    expect(screen.getByTestId('card-goal')).toHaveClass('hidden', '@md:inline-flex')
    expect(screen.getByTestId('card-prs')).toHaveClass('hidden', '@md:inline-flex')
    expect(screen.getByTestId('card-usage')).toHaveClass('hidden', '@md:inline-flex')
    expect(screen.getByTestId('card-model')).toHaveClass('hidden', '@md:inline-flex')
  })

  it('never gates content on a container width the sidebar cannot reach', () => {
    const { container } = renderCard(rich)
    const unreachable = container.querySelectorAll('[class*="@lg:"], [class*="@xl:"]')
    expect([...unreachable].map((el) => el.getAttribute('data-testid') ?? el.className)).toEqual([])
  })

  it('keeps the essentials (title, account chip, status phrase) ungated', () => {
    renderCard(rich)
    expect(within(screen.getByTestId('card-identity')).getByText('Fix login bug')).not.toHaveClass('hidden')
    expect(within(screen.getByTestId('card-meta')).getByText('work')).not.toHaveClass('hidden')
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
