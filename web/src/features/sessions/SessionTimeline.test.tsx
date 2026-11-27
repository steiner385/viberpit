/**
 * SessionTimeline (#947): a session's lifecycle timeline, collapsed. Hidden while
 * loading, on any error and with no events; otherwise one line per event, typed
 * spawn failures and auto-archives worded, unknown kinds shown by name.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import * as hooks from '@/api/hooks'
import { queryResult } from '@/test/reactQuery'
import SessionTimeline from './SessionTimeline'

vi.mock('@/api/hooks', () => ({ useSessionEvents: vi.fn() }))

const respond = (value: unknown) => vi.mocked(hooks.useSessionEvents).mockReturnValue(value as never)

const EVENTS = {
  events: [
    { id: 'e3', kind: 'auto_archived', ts: '2026-11-26T10:02:00Z',
      detail: { by: 'completion', reason: 'completion marker — goal met' } },
    { id: 'e2', kind: 'spawn_failed', ts: '2026-11-26T10:01:00Z',
      detail: { reason: 'network', transient: true, attempt: 2, requeued: false,
                message: 'Could not resolve host: github.com' } },
    { id: 'e1', kind: 'spawn_failed', ts: '2026-11-26T10:00:00Z',
      detail: { reason: 'network', transient: true, attempt: 1, requeued: true,
                message: 'Could not resolve host: github.com' } },
    { id: 'e0', kind: 'moved_host', ts: null, detail: {} },
  ],
}

describe('SessionTimeline', () => {
  beforeEach(() => vi.clearAllMocks())

  it('asks for the session it is given', () => {
    respond({ data: undefined })
    render(<SessionTimeline sessionId="s-1" />)
    expect(hooks.useSessionEvents).toHaveBeenCalledWith('s-1')
  })

  it.each([
    ['loading', { data: undefined, isLoading: true }],
    ['an error', { data: undefined, isError: true, error: new Error('500') }],
    ['no events', queryResult({ events: [] })],
    ['a body with no events array', queryResult([])],
  ])('renders nothing while %s', (_label, value) => {
    respond(value)
    const { container } = render(<SessionTimeline sessionId="s-1" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('is collapsed and counts its events', () => {
    respond(queryResult(EVENTS))
    render(<SessionTimeline sessionId="s-1" />)
    const details = screen.getByTestId('session-timeline')
    expect(details).not.toHaveAttribute('open')
    expect(screen.getByText('Timeline (4 events)')).toBeInTheDocument()
  })

  it('words each event, newest first', () => {
    respond(queryResult(EVENTS))
    render(<SessionTimeline sessionId="s-1" />)
    const items = screen.getAllByRole('listitem', { hidden: true })
    expect(items.map((li) => li.textContent)).toEqual([
      expect.stringContaining('Auto-archived by completion: completion marker — goal met'),
      expect.stringContaining('Failed to start (attempt 2): Could not resolve host: github.com'),
      expect.stringContaining('Failed to start (attempt 1): Could not resolve host: github.com — retrying'),
      'Moved',
    ])
  })

  it('words a dead-host adoption and how the new host took the session (#949)', () => {
    respond(queryResult({
      events: [
        { id: 'e5', kind: 'moved_host', ts: null, detail: { from: 'rocky', to: 'dobby', reason: 'host_lost', resume: 'failed' } },
        { id: 'e4', kind: 'moved_host', ts: null, detail: { from: 'rocky', to: 'dobby', reason: 'host_lost', resume: 'deferred' } },
        { id: 'e3', kind: 'moved_host', ts: null, detail: { from: 'rocky', to: 'dobby', reason: 'host_lost', resume: 'queued' } },
        { id: 'e2', kind: 'moved_host', ts: null, detail: { from: 'rocky', to: 'dobby', reason: 'host_lost', resume: 'fresh' } },
        { id: 'e1', kind: 'moved_host', ts: null, detail: { from: 'rocky', to: 'dobby', reason: 'host_lost', resume: 'transcript' } },
        { id: 'e0', kind: 'interrupted', ts: null, detail: { kind: 'host_lost', fromStatus: 'in_progress', host: 'rocky' } },
      ],
    }))
    render(<SessionTimeline sessionId="s-1" />)
    const items = screen.getAllByRole('listitem', { hidden: true })
    expect(items.map((li) => li.textContent)).toEqual([
      'Moved from rocky to dobby: could not be resumed there',
      'Moved from rocky to dobby: resumes on its next prompt',
      'Moved from rocky to dobby: waiting for a slot there',
      'Moved from rocky to dobby: conversation could not be recovered — started over with context',
      'Moved from rocky to dobby: conversation resumed',
      'Interrupted: host rocky became unreachable (was in progress)',
    ])
  })

  it('words a lost hold, an account rotation and a delete retry (#947 phase 2)', () => {
    respond(queryResult({
      events: [
        { id: 'e5', kind: 'delete_retry', ts: null, detail: { strike: 3, error: 'EBUSY: worktree locked', stuck: true } },
        { id: 'e4', kind: 'delete_retry', ts: null, detail: { strike: 1, error: 'EBUSY: worktree locked', stuck: false } },
        { id: 'e3', kind: 'rotated_account', ts: null, detail: { from: 'alice', reason: 'rate_limited', attempt: 2 } },
        { id: 'e2', kind: 'hold_lost', ts: null,
          detail: { kind: 'permission', holdId: 'h1', createdAt: null, content: { tool: 'Bash', input: { command: 'ls' } } } },
        { id: 'e1', kind: 'hold_lost', ts: null, detail: { kind: 'question', holdId: 'h2', createdAt: null, content: {} } },
        { id: 'e0', kind: 'hold_lost', ts: null, detail: { kind: 'plan', holdId: 'h3', createdAt: null, content: {} } },
      ],
    }))
    render(<SessionTimeline sessionId="s-1" />)
    const items = screen.getAllByRole('listitem', { hidden: true })
    expect(items.map((li) => li.textContent)).toEqual([
      'Delete retry #3 — stuck: EBUSY: worktree locked',
      'Delete retry #1: EBUSY: worktree locked',
      'Rotated off alice: rate_limited (attempt 2)',
      'A pending permission request was lost in a restart',
      'A pending question was lost in a restart',
      'A pending plan approval was lost in a restart',
    ])
  })

  it('still words each new kind when its detail is incomplete', () => {
    respond(queryResult({
      events: [
        { id: 'e2', kind: 'rotated_account', ts: null, detail: { reason: 'quota exhausted' } },
        { id: 'e1', kind: 'delete_retry', ts: null, detail: { strike: 'two', error: 'EBUSY', stuck: false } },
        { id: 'e0', kind: 'hold_lost', ts: null, detail: { content: {} } },
      ],
    }))
    render(<SessionTimeline sessionId="s-1" />)
    const items = screen.getAllByRole('listitem', { hidden: true })
    expect(items.map((li) => li.textContent)).toEqual([
      'Rotated account: quota exhausted',
      'Delete retry: EBUSY',
      'A pending hold was lost in a restart',
    ])
  })
})
