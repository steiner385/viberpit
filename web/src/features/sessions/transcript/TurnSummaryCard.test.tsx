import { render, screen, fireEvent, within } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { TurnSummaryCard } from './TurnSummaryCard'
import { summaryOutcome, type ChangedFile, type SummaryOutcome } from '@/session/turnSummary'

const REASON = 'Need a decision on whether to override the read-only constraint'

function outcome(status: string, reason: string | null = null, suspected: string | null = null, busy = false): SummaryOutcome {
  const o = summaryOutcome(status, reason, suspected, busy)
  if (!o) throw new Error(`no card for ${status}`)
  return o
}

function card(o: SummaryOutcome, extra: Partial<React.ComponentProps<typeof TurnSummaryCard>> = {}) {
  const onPrompt = vi.fn()
  const utils = render(<TurnSummaryCard outcome={o} files={[]} suggestions={[]} onPrompt={onPrompt} {...extra} />)
  return { ...utils, onPrompt, section: screen.getByRole('region', { name: 'Turn summary' }) }
}

describe('TurnSummaryCard — outcome', () => {
  it.each([
    ['complete', 'Complete'],
    ['ended', 'Complete'],
    ['done', 'Your turn'],
    ['needs_attention', 'Turn failed'],
    ['limited', 'Usage limit'],
  ])('is a "Turn summary" section that leads with the %s label', (status, label) => {
    const { section } = card(outcome(status))
    expect(section.tagName).toBe('SECTION')
    expect(section).toHaveTextContent(label)
    // "Done" means the turn, never the objective — the card never says it.
    expect(section.textContent ?? '').not.toMatch(/\bdone\b/i)
  })

  it('leads a hand-back with the session\'s own reason', () => {
    const { section } = card(outcome('handed_back', REASON))
    expect(section).toHaveAttribute('data-mode', 'handed_back')
    expect(section).toHaveTextContent('Handed back')
    expect(section).toHaveTextContent(REASON)
  })

  it('says plainly that the session stopped when it handed back without a reason', () => {
    const { section } = card(outcome('handed_back'))
    expect(section).toHaveTextContent(/stopped and is waiting on you/i)
  })

  it('does not show a stale hand-back reason beside a finished turn', () => {
    const { section } = card(outcome('done', REASON))
    expect(section).not.toHaveTextContent(REASON)
  })
})

describe('TurnSummaryCard — replies', () => {
  it('renders each cached suggestion as a one-tap reply that sends immediately', () => {
    const { onPrompt } = card(outcome('done'), { suggestions: ['Run the tests', 'Proceed with Phase B'] })
    fireEvent.click(screen.getByRole('button', { name: 'Proceed with Phase B' }))
    expect(onPrompt).toHaveBeenCalledWith('Proceed with Phase B', [], undefined, 'suggested')
  })

  it('offers one-tap replies on a hand-back too', () => {
    const { onPrompt } = card(outcome('handed_back', REASON), { suggestions: ['Keep it read-only'] })
    fireEvent.click(screen.getByRole('button', { name: 'Keep it read-only' }))
    expect(onPrompt).toHaveBeenCalledWith('Keep it read-only', [], undefined, 'suggested')
  })

  it('falls back to a review-and-reply hint only when no suggestions are cached', () => {
    const a = card(outcome('done'))
    expect(screen.getByText(/review the last message above, then reply below/i)).toBeInTheDocument()
    a.unmount()
    card(outcome('done'), { suggestions: ['Continue'] })
    expect(screen.queryByText(/review the last message above/i)).toBeNull()
  })

  it('offers no reply hint on a finished objective', () => {
    card(outcome('complete'))
    expect(screen.queryByText(/then reply below/i)).toBeNull()
  })
})

describe('TurnSummaryCard — likely complete', () => {
  it('shows the hint with Mark complete / Not done, which answer confirm / reject', () => {
    const onResolve = vi.fn()
    card(outcome('handed_back', 'PR #2521 is open', 'PR #2521 open with auto-merge armed'), {
      onResolveSuspectedComplete: onResolve,
    })
    expect(screen.getByText('Likely complete')).toBeInTheDocument()
    expect(screen.getByText(/PR #2521 open with auto-merge armed/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /mark complete/i }))
    expect(onResolve).toHaveBeenCalledWith('confirm')
    fireEvent.click(screen.getByRole('button', { name: /not done/i }))
    expect(onResolve).toHaveBeenCalledWith('reject')
  })

  it('shows no completion buttons when nothing suspects the objective is met', () => {
    card(outcome('handed_back', REASON), { onResolveSuspectedComplete: vi.fn() })
    expect(screen.queryByRole('button', { name: /mark complete/i })).toBeNull()
    expect(screen.queryByText(/likely complete/i)).toBeNull()
  })

  it('disables both answers while the decision is in flight', () => {
    card(outcome('handed_back', null, 'PR #9 merged'), { onResolveSuspectedComplete: vi.fn(), resolvePending: true })
    expect(screen.getByRole('button', { name: /mark complete/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /not done/i })).toBeDisabled()
  })
})

// #2647: a hand_back made mid-turn is a live ask while the turn still runs.
describe('TurnSummaryCard — asked you (busy with a hand-back reason)', () => {
  it('notes the ask, says the turn is still working, and keeps the one-tap replies', () => {
    const { section } = card(outcome('in_progress', REASON, 'PR #1 open', true), { suggestions: ['Approved'] })
    expect(section).toHaveAttribute('data-mode', 'asked')
    expect(section).toHaveTextContent('Asked you')
    expect(section).toHaveTextContent(REASON)
    expect(section).toHaveTextContent(/still working/i)
    expect(screen.getByRole('button', { name: 'Approved' })).toBeInTheDocument()
    // The hint judges a finished turn, so it never shows mid-turn.
    expect(screen.queryByText(/likely complete/i)).toBeNull()
  })
})

describe('TurnSummaryCard — files', () => {
  const counted = (k: number): ChangedFile[] =>
    Array.from({ length: k }, (_, i) => ({ path: `/repo/src/f${i}.ts`, plus: i + 1, minus: 1 }))

  it('lists at most five paths, then "+K more", with the summed +N −M', () => {
    const { section } = card(outcome('done'), { files: counted(7) })
    const list = within(section).getByRole('list', { name: 'Changed files' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(5)
    expect(list).toHaveTextContent('/repo/src/f0.ts')
    expect(list).not.toHaveTextContent('/repo/src/f5.ts')
    expect(section).toHaveTextContent('+2 more')
    expect(section).toHaveTextContent('7 files changed')
    // 1+2+…+7 = 28 added, 7 removed.
    expect(section).toHaveTextContent('+28 −7')
  })

  it('shows the file count without +/− when a path is uncounted (Codex)', () => {
    const { section } = card(outcome('done'), { files: [{ path: '/r/a.ts', plus: 2, minus: 1 }, { path: '/r/c.ts' }] })
    expect(section).toHaveTextContent('2 files changed')
    expect(section).not.toHaveTextContent('+2 −1')
    expect(section).not.toHaveTextContent('−')
  })

  it('says "1 file changed" for one path', () => {
    const { section } = card(outcome('done'), { files: counted(1) })
    expect(section).toHaveTextContent('1 file changed')
  })

  it('omits the files section when the turn changed no files', () => {
    const { section } = card(outcome('done'), { onOpenChanges: vi.fn() })
    expect(within(section).queryByRole('list', { name: 'Changed files' })).toBeNull()
    expect(within(section).queryByRole('button', { name: 'Open Changes' })).toBeNull()
  })

  it('Open Changes calls through', () => {
    const onOpenChanges = vi.fn()
    card(outcome('complete'), { files: counted(2), onOpenChanges })
    fireEvent.click(screen.getByRole('button', { name: 'Open Changes' }))
    expect(onOpenChanges).toHaveBeenCalledTimes(1)
  })

  it('offers no Open Changes when the caller has no Changes view to open', () => {
    card(outcome('complete'), { files: counted(2) })
    expect(screen.queryByRole('button', { name: 'Open Changes' })).toBeNull()
  })
})
