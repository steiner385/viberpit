/**
 * LostHoldBanner (#947, spec 002 FR-406): the content of a hold whose waiter died with
 * the daemon, shown prominently with the "Ask again" affordance. The prompt it builds
 * never carries a decision (FR-306: the system never answers a hold on the user's behalf).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { LostHold } from '@/api/types'
import LostHoldBanner from './LostHoldBanner'
import { askAgainPrompt, holdKindLabel, lostHoldHeading, summarizeLostHold } from './lostHold'

const PERMISSION: LostHold = {
  kind: 'permission', holdId: 'h1', createdAt: '2026-11-26T10:00:00Z',
  content: { tool: 'Bash', input: { command: 'rm -rf build/' }, items: null, turnId: 't1', permId: 'p1' },
}
const FILE_PERMISSION: LostHold = {
  kind: 'permission', holdId: 'h2', createdAt: null,
  content: { tool: 'Edit', input: { file_path: '/repo/src/app.py', old_string: 'a', new_string: 'b' }, turnId: 't1', permId: 'p2' },
}
const BATCH_PERMISSION: LostHold = {
  kind: 'permission', holdId: 'h3', createdAt: null,
  content: { tool: 'mcp__viberpit__archive_sessions', input: { ids: ['s1', 's2'] }, items: ['Fix login', 'Bump deps'], turnId: 't1', permId: 'p3' },
}
const QUESTION: LostHold = {
  kind: 'question', holdId: 'h4', createdAt: '2026-11-26T10:00:00Z',
  content: {
    turnId: 't2',
    questions: [
      { questionId: 'q1', header: 'Database', question: 'Which database should the service use?', multiSelect: false,
        options: [{ label: 'Postgres', description: 'prod parity' }, { label: 'SQLite', description: 'simplest' }] },
      { questionId: 'q2', header: 'Tests', question: 'Run the slow suite too?', multiSelect: false,
        options: [{ label: 'Yes' }, { label: 'No' }] },
    ],
  },
}
const PLAN: LostHold = {
  kind: 'plan', holdId: 'h5', createdAt: null,
  content: { tool: 'ExitPlanMode', input: { plan: '# Plan\n\n1. Add the column\n2. Backfill it\n' + 'x'.repeat(400) }, turnId: 't3', permId: 'p5' },
}
/** A record whose kind the server failed to write — must never crash the banner. */
const KINDLESS = { kind: null, content: { tool: 'Bash', input: { command: 'ls' } } } as unknown as LostHold

describe('holdKindLabel', () => {
  it.each([
    ['permission', 'permission request'],
    ['question', 'question'],
    ['plan', 'plan approval'],
    ['something_else', 'something else'],
    [null, 'hold'],
    [undefined, 'hold'],
    ['', 'hold'],
    [42, 'hold'],
  ])('names %s', (kind, label) => {
    expect(holdKindLabel(kind)).toBe(label)
  })
})

describe('summarizeLostHold', () => {
  it('summarises a Bash permission by its command', () => {
    expect(summarizeLostHold(PERMISSION)).toBe('Bash: rm -rf build/')
  })

  it('summarises a file-tool permission by its path', () => {
    expect(summarizeLostHold(FILE_PERMISSION)).toBe('Edit: /repo/src/app.py')
  })

  it('lists a batch permission by its items', () => {
    expect(summarizeLostHold(BATCH_PERMISSION)).toBe('mcp__viberpit__archive_sessions: Fix login, Bump deps')
  })

  it('lists each question with its header, text and option labels only, marked as options', () => {
    const s = summarizeLostHold(QUESTION)
    expect(s).toContain('Database: Which database should the service use? (options: Postgres / SQLite)')
    expect(s).toContain('Tests: Run the slow suite too? (options: Yes / No)')
    expect(s).not.toContain('prod parity')
  })

  it('marks even a single option as an option, so it cannot read as the answer', () => {
    const s = summarizeLostHold({
      kind: 'question',
      content: { questions: [{ header: 'Go', question: 'Continue?', options: [{ label: 'Yes' }] }] },
    })
    expect(s).toBe('Go: Continue? (options: Yes)')
  })

  it('leads a plan with its first 200 characters of plan text', () => {
    const s = summarizeLostHold(PLAN)
    expect(s.startsWith('# Plan')).toBe(true)
    expect(s.endsWith('…')).toBe(true)
    expect(s.length).toBeLessThanOrEqual(201)
  })

  it('caps every other line at 300 characters', () => {
    const long = 'a'.repeat(1000)
    const cmd = summarizeLostHold({ kind: 'permission', content: { tool: 'Bash', input: { command: long } } })
    expect(cmd.length).toBeLessThanOrEqual('Bash: '.length + 301)
    expect(cmd.endsWith('…')).toBe(true)
    const q = summarizeLostHold({
      kind: 'question',
      content: { questions: [{ header: 'H', question: long, options: [{ label: long }] }] },
    })
    for (const piece of q.split(' (options: ')) expect(piece.length).toBeLessThanOrEqual(303)
    const items = summarizeLostHold({ kind: 'permission', content: { tool: 'T', items: [long, long, long] } })
    expect(items.length).toBeLessThanOrEqual('T: '.length + 301)
  })

  it('is empty when the content carries nothing it can word', () => {
    expect(summarizeLostHold({ kind: 'plan', content: {} })).toBe('')
  })

  it('still words a record with no kind as a tool call', () => {
    expect(summarizeLostHold(KINDLESS)).toBe('Bash: ls')
  })
})

describe('lostHoldHeading', () => {
  it('is the kind label, flagged when the server truncated the content', () => {
    expect(lostHoldHeading(PERMISSION)).toBe('permission request')
    expect(lostHoldHeading({ ...PERMISSION, content: { ...PERMISSION.content, truncated: true } }))
      .toBe('permission request (truncated)')
    expect(lostHoldHeading(KINDLESS)).toBe('hold')
  })
})

describe('askAgainPrompt', () => {
  it('names the kind and summary, and says nothing was approved or answered', () => {
    const p = askAgainPrompt(PERMISSION)
    expect(p).toContain('permission request')
    expect(p).toContain('Bash: rm -rf build/')
    expect(p).toContain('nothing was approved or answered')
    expect(p).toContain('raise it again')
  })

  it('never answers on the user\'s behalf (FR-306): no decision in the prompt', () => {
    for (const hold of [PERMISSION, QUESTION, PLAN, KINDLESS]) {
      const p = askAgainPrompt(hold)
      // The summary echoes option LABELS (e.g. "Yes / No") as the question text; the
      // instruction lines around it must not pick one.
      const instructions = p.split('\n').filter((line) => !summarizeLostHold(hold).includes(line))
      for (const line of instructions) {
        expect(line.toLowerCase()).not.toMatch(/\b(approve|approved:|allow|deny|yes|no|go ahead|proceed)\b/)
      }
    }
  })

  it('omits the empty summary line when there is nothing to quote', () => {
    const p = askAgainPrompt({ kind: 'plan', content: {} })
    expect(p).not.toMatch(/\n\n/)
    expect(p).toContain('plan approval')
  })

  it('carries the truncation flag and survives a missing kind', () => {
    expect(askAgainPrompt({ ...PLAN, content: { ...PLAN.content, truncated: true } }))
      .toContain('waiting on a plan approval (truncated) that I never saw')
    expect(askAgainPrompt(KINDLESS)).toContain('waiting on a hold that I never saw')
  })
})

describe('LostHoldBanner', () => {
  it('shows a lost permission with its tool and command', () => {
    render(<LostHoldBanner lostHold={PERMISSION} onAskAgain={vi.fn()} />)
    const el = screen.getByTestId('lost-hold-banner')
    expect(el).toHaveTextContent('When the daemon restarted, the agent was waiting on a permission request:')
    expect(el).toHaveTextContent('Bash: rm -rf build/')
  })

  it('shows a lost question with each question and its option labels', () => {
    render(<LostHoldBanner lostHold={QUESTION} onAskAgain={vi.fn()} />)
    const el = screen.getByTestId('lost-hold-banner')
    expect(el).toHaveTextContent('waiting on a question:')
    expect(el).toHaveTextContent('Database: Which database should the service use?')
    expect(el).toHaveTextContent('(options: Postgres / SQLite)')
    expect(el).toHaveTextContent('Tests: Run the slow suite too?')
    expect(el).not.toHaveTextContent('prod parity')
  })

  it('shows a lost plan approval with the start of the plan', () => {
    render(<LostHoldBanner lostHold={PLAN} onAskAgain={vi.fn()} />)
    const el = screen.getByTestId('lost-hold-banner')
    expect(el).toHaveTextContent('waiting on a plan approval:')
    expect(el).toHaveTextContent('# Plan 1. Add the column')
  })

  it('flags server-side truncation in the heading', () => {
    render(<LostHoldBanner
      lostHold={{ ...PERMISSION, content: { ...PERMISSION.content, truncated: true } }}
      onAskAgain={vi.fn()}
    />)
    expect(screen.getByTestId('lost-hold-banner'))
      .toHaveTextContent('waiting on a permission request (truncated):')
  })

  it('renders a record with no kind without crashing', () => {
    render(<LostHoldBanner lostHold={KINDLESS} onAskAgain={vi.fn()} />)
    const el = screen.getByTestId('lost-hold-banner')
    expect(el).toHaveTextContent('waiting on a hold:')
    expect(el).toHaveTextContent('Bash: ls')
  })

  it('renders content as text, never markup', () => {
    render(<LostHoldBanner
      lostHold={{ kind: 'permission', content: { tool: 'Bash', input: { command: '<img src=x onerror=alert(1)>' } } }}
      onAskAgain={vi.fn()}
    />)
    expect(screen.getByTestId('lost-hold-banner').querySelector('img')).toBeNull()
    expect(screen.getByTestId('lost-hold-banner')).toHaveTextContent('<img src=x onerror=alert(1)>')
  })

  it('scopes the live region to the copy, not the button', () => {
    render(<LostHoldBanner lostHold={PERMISSION} onAskAgain={vi.fn()} />)
    const status = screen.getByRole('status')
    expect(status.textContent?.toLowerCase()).toContain('permission')
    expect(status.textContent?.toLowerCase()).not.toContain('ask again')
    expect(screen.getByTestId('lost-hold-banner')).not.toHaveAttribute('role')
  })

  it('offers no approve / deny / answer control — only Ask again', () => {
    render(<LostHoldBanner lostHold={QUESTION} onAskAgain={vi.fn()} />)
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Ask again' })).toBeInTheDocument()
  })

  it('asks again with the full recovery prompt on click', () => {
    const onAskAgain = vi.fn()
    render(<LostHoldBanner lostHold={PERMISSION} onAskAgain={onAskAgain} />)
    fireEvent.click(screen.getByRole('button', { name: 'Ask again' }))
    expect(onAskAgain).toHaveBeenCalledTimes(1)
    expect(onAskAgain).toHaveBeenCalledWith(askAgainPrompt(PERMISSION))
  })
})
