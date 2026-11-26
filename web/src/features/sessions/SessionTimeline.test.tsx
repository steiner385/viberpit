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
      'moved host',
    ])
  })
})
