import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'
import { session } from '@/test/factories'

// Model chip on line 1: which model a session is currently running, readable
// from the list without opening the session. `session.model` is the record's
// chosen alias/pinned id (null = account default); the chip renders it through
// the shared `tierLabel` so the list agrees with the session header and the
// approval panel about what a model is called.

const base: Session = session({
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'in_progress',
  createdAt: '2026-06-22T11:00:00Z',
})

const noop = () => {}

function renderCard(s: Session) {
  return render(
    <MemoryRouter>
      <SessionCard
        session={s}
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

describe('SessionCard model chip', () => {
  it('shows the tier label for a bare alias', () => {
    renderCard({ ...base, model: 'opus' })
    const chip = screen.getByTestId('card-model')
    expect(chip).toHaveTextContent('Opus')
    expect(chip).toHaveAttribute('title', 'Model: Opus')
  })

  it('shows the friendly full name for a pinned model id', () => {
    renderCard({ ...base, model: 'claude-haiku-4-5' })
    expect(screen.getByTestId('card-model')).toHaveTextContent('Haiku 4.5')
  })

  it('keeps a [1m] context tag legible', () => {
    renderCard({ ...base, model: 'opus[1m]' })
    // tierLabel returns unknown shapes verbatim; the chip must not blank them.
    expect(screen.getByTestId('card-model')).toHaveTextContent('opus[1m]')
  })

  it('renders nothing when the session runs the account default (null model)', () => {
    renderCard({ ...base, model: null })
    expect(screen.queryByTestId('card-model')).not.toBeInTheDocument()
  })

  it('renders nothing when the record predates the model field', () => {
    renderCard(base)
    expect(screen.queryByTestId('card-model')).not.toBeInTheDocument()
  })

  it('lives on the meta row next to the account chip and is wide-card only', () => {
    renderCard({ ...base, model: 'sonnet' })
    const meta = screen.getByTestId('card-meta')
    const chip = within(meta).getByTestId('card-model')
    expect(chip).toHaveClass('hidden', '@md:inline-flex')
    // Model directly precedes the account chip so the pair reads together.
    expect(chip.nextElementSibling).toHaveTextContent('work')
  })

  it('flags a switch queued for the end of the current turn', () => {
    renderCard({ ...base, model: 'opus', modelPending: true })
    expect(screen.getByTestId('card-model')).toHaveAttribute(
      'title',
      'Model: Opus (switching after this turn)',
    )
  })

  it('flags a parked upgrade awaiting approval', () => {
    renderCard({ ...base, model: 'sonnet', pendingModel: 'fable' })
    expect(screen.getByTestId('card-model')).toHaveAttribute(
      'title',
      'Model: Sonnet (Fable requested, awaiting approval)',
    )
  })

  it('tints the gated tier so an expensive session stands out', () => {
    renderCard({ ...base, model: 'fable' })
    expect(screen.getByTestId('card-model').className).toMatch(/violet/)
  })

  it('keeps ordinary tiers on the neutral chip palette', () => {
    renderCard({ ...base, model: 'sonnet' })
    expect(screen.getByTestId('card-model').className).not.toMatch(/violet/)
  })

  it('labels a Codex model through the same chip', () => {
    renderCard({ ...base, engine: 'codex', model: 'gpt-5.1-codex' })
    const chip = screen.getByTestId('card-model')
    expect(chip).toHaveTextContent('GPT-5.1 Codex')
    expect(chip).toHaveAttribute('title', 'Model: GPT-5.1 Codex')
  })

  it('never tints a Codex model as the gated tier', () => {
    renderCard({ ...base, engine: 'codex', model: 'gpt-5.1-codex' })
    expect(screen.getByTestId('card-model').className).not.toMatch(/violet/)
  })
})

// ── engine + reasoning effort (session identity, Task 5.3) ───────────────────

describe('SessionCard engine chip', () => {
  it('names the agent when it is not the default Claude Code engine', () => {
    renderCard({ ...base, engine: 'codex' })
    const chip = screen.getByTestId('card-engine')
    expect(chip).toHaveTextContent('Codex')
    expect(chip).toHaveAttribute('title', 'Agent: Codex')
  })

  it('renders nothing for a Claude session, so a Claude-only fleet gains no chip', () => {
    renderCard({ ...base, engine: 'claude_code' })
    expect(screen.queryByTestId('card-engine')).not.toBeInTheDocument()
  })

  it('is wide-card only, like every other secondary signal', () => {
    renderCard({ ...base, engine: 'codex' })
    expect(screen.getByTestId('card-engine')).toHaveClass('hidden', '@md:inline-flex')
  })
})

describe('SessionCard reasoning-effort chip', () => {
  it('shows the engine-native reasoning choice', () => {
    renderCard({ ...base, engine: 'codex', reasoningEffort: 'high' })
    const chip = screen.getByTestId('card-effort')
    expect(chip).toHaveTextContent('high')
    expect(chip).toHaveAttribute('title', 'Reasoning effort: high')
  })

  it('renders nothing when the session runs the model default', () => {
    renderCard({ ...base, engine: 'codex', reasoningEffort: null })
    expect(screen.queryByTestId('card-effort')).not.toBeInTheDocument()
    renderCard(base)
    expect(screen.queryByTestId('card-effort')).not.toBeInTheDocument()
  })

  it('is wide-card only', () => {
    renderCard({ ...base, engine: 'codex', reasoningEffort: 'medium' })
    expect(screen.getByTestId('card-effort')).toHaveClass('hidden', '@md:inline-flex')
  })
})

// ── mixed fleet + overflow (Task 5.3 regression) ─────────────────────────────

describe('SessionCard in a mixed Claude/Codex list', () => {
  const claude = session({
    id: 'c1', name: 'Claude one', accountName: 'work', model: 'opus', status: 'idle',
  })
  const codex = session({
    id: 'x1', name: 'Codex one', accountName: 'chatgpt-pro', engine: 'codex',
    model: 'gpt-5.1-codex', reasoningEffort: 'high', status: 'idle',
  })

  it('renders each card with its own agent, model and account', () => {
    render(
      <MemoryRouter>
        {[claude, codex].map((s) => (
          <SessionCard
            key={s.id} session={s} account={undefined}
            onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} onQuickResume={noop}
          />
        ))}
      </MemoryRouter>,
    )
    const cards = screen.getAllByTestId('card-meta')
    expect(within(cards[0]).queryByTestId('card-engine')).toBeNull()
    expect(within(cards[0]).getByTestId('card-model')).toHaveTextContent('Opus')
    expect(within(cards[1]).getByTestId('card-engine')).toHaveTextContent('Codex')
    expect(within(cards[1]).getByTestId('card-model')).toHaveTextContent('GPT-5.1 Codex')
    expect(within(cards[1]).getByTestId('card-effort')).toHaveTextContent('high')
  })

  it('truncates long account and model names instead of overflowing the row', () => {
    renderCard({
      ...base,
      accountName: 'a-really-long-account-alias@some-organisation.example.com',
      engine: 'codex',
      model: 'gpt-5.1-codex-with-an-absurdly-long-suffix',
      reasoningEffort: 'extremely-high-effort-setting',
    })
    // The always-visible account chip keeps its cap + truncation …
    const account = screen.getByTestId('card-account')
    expect(account).toHaveClass('max-w-[50%]', 'truncate')
    expect(account).toHaveAttribute('title', expect.stringContaining('a-really-long-account-alias'))
    // … and the @md-gated chips truncate too, so neither can starve the title.
    for (const id of ['card-model', 'card-effort', 'card-engine']) {
      expect(screen.getByTestId(id), id).toHaveClass('truncate')
    }
  })
})
