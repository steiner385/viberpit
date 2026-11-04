import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import type { SearchHit, TranscriptSearchResult } from '@/api/transcript'
import TranscriptSearchBar from './TranscriptSearchBar'

const fetchTranscriptSearch = vi.fn<(q: string) => Promise<TranscriptSearchResult>>()

const hit = (n: number): SearchHit => ({
  turnId: `t${n}`, ordinal: n, role: 'assistant', snippet: `hit ${n}`, matchStart: 0, matchEnd: 3,
})
const result = (count: number, truncated = false): TranscriptSearchResult =>
  ({ hits: Array.from({ length: count }, (_, i) => hit(i)), truncated, totalTurns: 50 })
/** The request a jump to hit `n` carries. */
const to = (n: number) => expect.objectContaining({ turnId: `t${n}`, ordinal: n, role: 'assistant' })

/** A promise the test resolves by hand, to order two responses. */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

function renderBar(over: Partial<React.ComponentProps<typeof TranscriptSearchBar>> = {}) {
  const props = {
    search: fetchTranscriptSearch, earliestTurn: 0, focusSignal: 0,
    onClose: vi.fn(), onJump: vi.fn(), onTerms: vi.fn(), onError: vi.fn(), ...over,
  }
  const view = render(<TranscriptSearchBar {...props} />)
  const input = screen.getByRole('searchbox', { name: 'Search this transcript' })
  return { ...view, props, input }
}

/** Type `q`, let the 250 ms debounce fire, and settle the response. */
async function search(input: HTMLElement, q: string) {
  fireEvent.change(input, { target: { value: q } })
  await act(async () => { vi.advanceTimersByTime(250) })
}

beforeEach(() => {
  vi.useFakeTimers()
  fetchTranscriptSearch.mockReset()
})
afterEach(() => { vi.useRealTimers() })

describe('TranscriptSearchBar', () => {
  it('focuses its input on open', () => {
    const { input } = renderBar()
    expect(input).toHaveFocus()
  })

  it('debounces 250 ms and never searches under 2 characters', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(1))
    const { input } = renderBar()
    fireEvent.change(input, { target: { value: 'w' } })
    await act(async () => { vi.advanceTimersByTime(1000) })
    expect(fetchTranscriptSearch).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: 'wi' } })
    await act(async () => { vi.advanceTimersByTime(200) })
    fireEvent.change(input, { target: { value: 'wid' } })
    await act(async () => { vi.advanceTimersByTime(200) })
    expect(fetchTranscriptSearch).not.toHaveBeenCalled()
    await act(async () => { vi.advanceTimersByTime(50) })
    expect(fetchTranscriptSearch).toHaveBeenCalledTimes(1)
    expect(fetchTranscriptSearch).toHaveBeenCalledWith('wid')
  })

  it('reports the answered query\'s terms to highlight, and none when the query is cut short', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(1))
    const { input, props } = renderBar()
    await search(input, ' Foo  Bar ')
    expect(props.onTerms).toHaveBeenLastCalledWith(['foo', 'bar'])
    fireEvent.change(input, { target: { value: 'f' } })
    expect(props.onTerms).toHaveBeenLastCalledWith([])
  })

  it('selects and jumps to the NEWEST hit when results arrive, if it is already loaded', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(3))
    const { input, props } = renderBar({ earliestTurn: 2 })
    await search(input, 'hit')
    expect(screen.getByText('3 of 3')).toBeInTheDocument()
    expect(props.onJump).toHaveBeenCalledTimes(1)
    expect(props.onJump).toHaveBeenLastCalledWith(to(2))
  })

  it('reports the new query\'s terms BEFORE its auto-jump — the terms reset the current hit', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(2))
    const order: string[] = []
    const { input } = renderBar({
      onTerms: (terms) => { if (terms.length) order.push('terms') },
      onJump: (req) => { order.push(`jump:${req.turnId}:${req.terms.join(',')}`) },
    })
    await search(input, 'hit')
    expect(order).toEqual(['terms', 'jump:t1:hit'])
  })

  it('never auto-jumps to a hit that would need paging — that waits for Enter', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(3))
    const { input, props } = renderBar({ earliestTurn: 3 })
    await search(input, 'hit')
    expect(screen.getByText('0 of 3')).toBeInTheDocument()
    expect(props.onJump).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter' }) // the first Enter goes to the newest
    expect(props.onJump).toHaveBeenLastCalledWith(to(2))
    expect(screen.getByText('3 of 3')).toBeInTheDocument()
  })

  it('Enter steps OLDER and Shift+Enter NEWER, both wrapping', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(3))
    const { input, props } = renderBar()
    await search(input, 'hit') // auto: 3 of 3
    const enter = (shiftKey = false) => fireEvent.keyDown(input, { key: 'Enter', shiftKey })
    enter()
    expect(props.onJump).toHaveBeenLastCalledWith(to(1))
    expect(screen.getByText('2 of 3')).toBeInTheDocument()
    enter()
    expect(props.onJump).toHaveBeenLastCalledWith(to(0))
    enter() // wraps past the oldest to the newest
    expect(props.onJump).toHaveBeenLastCalledWith(to(2))
    expect(screen.getByText('3 of 3')).toBeInTheDocument()
    enter(true) // wraps past the newest to the oldest
    expect(props.onJump).toHaveBeenLastCalledWith(to(0))
    expect(screen.getByText('1 of 3')).toBeInTheDocument()
  })

  it('carries each hit\'s rank among the hits sharing its turn', async () => {
    const same = (snippet: string): SearchHit => ({ ...hit(4), snippet })
    fetchTranscriptSearch.mockResolvedValue({ hits: [same('hit a'), same('hit b')], truncated: false })
    const { input, props } = renderBar()
    await search(input, 'hit')
    expect(props.onJump).toHaveBeenLastCalledWith(expect.objectContaining({ turnId: 't4', rank: 1 }))
  })

  it('↑ steps older like Enter, ↓ newer like Shift+Enter — and say so', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(2))
    const { input, props } = renderBar()
    await search(input, 'hit') // auto: 2 of 2
    const older = screen.getByRole('button', { name: 'Older match' })
    const newer = screen.getByRole('button', { name: 'Newer match' })
    expect(older).toHaveAttribute('title', expect.stringContaining('Enter'))
    expect(newer).toHaveAttribute('title', expect.stringContaining('Shift+Enter'))
    fireEvent.click(older)
    expect(props.onJump).toHaveBeenLastCalledWith(to(0))
    fireEvent.click(newer)
    expect(props.onJump).toHaveBeenLastCalledWith(to(1))
  })

  it('shows M+ when older matches were cut', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(200, true))
    const { input } = renderBar({ earliestTurn: 500 })
    await search(input, 'hit')
    expect(screen.getByText('0 of 200+')).toBeInTheDocument()
  })

  it('says so when nothing matches', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(0))
    const { input, props } = renderBar()
    await search(input, 'zzz')
    expect(screen.getByText('No matches')).toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onJump).not.toHaveBeenCalled()
  })

  it('a failed search says so in the status and reports the error', async () => {
    fetchTranscriptSearch.mockRejectedValueOnce(new Error('boom'))
    const { input, props } = renderBar()
    await search(input, 'widget')
    expect(props.onError).toHaveBeenCalledWith('Couldn’t search this conversation')
    expect(screen.getByText('Search failed')).toBeInTheDocument()
    expect(screen.queryByText(/ of /)).toBeNull()
  })

  it('discards a stale response that lands after a newer query\'s', async () => {
    const first = deferred<TranscriptSearchResult>()
    const second = deferred<TranscriptSearchResult>()
    fetchTranscriptSearch.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { input, props } = renderBar({ earliestTurn: 50 })
    await search(input, 'old')
    await search(input, 'new')
    await act(async () => { second.resolve(result(2)) })
    expect(screen.getByText('0 of 2')).toBeInTheDocument()
    await act(async () => { first.resolve(result(5)) })
    expect(screen.getByText('0 of 2')).toBeInTheDocument()
    expect(props.onTerms).toHaveBeenLastCalledWith(['new'])
  })

  it('discards a response for a query that has since been shortened below 2 characters', async () => {
    const pending = deferred<TranscriptSearchResult>()
    fetchTranscriptSearch.mockReturnValueOnce(pending.promise)
    const { input, props } = renderBar()
    await search(input, 'old')
    fireEvent.change(input, { target: { value: 'o' } })
    await act(async () => { pending.resolve(result(4)) })
    expect(screen.queryByText(/of 4/)).toBeNull()
    expect(props.onJump).not.toHaveBeenCalled()
  })

  it('Esc closes, and is consumed so the shell does not also detach or stop', () => {
    const { input, props } = renderBar()
    const ev = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    input.dispatchEvent(ev)
    expect(props.onClose).toHaveBeenCalledTimes(1)
    expect(ev.defaultPrevented).toBe(true)
  })

  it('✕ closes', () => {
    const { props } = renderBar()
    fireEvent.click(screen.getByRole('button', { name: 'Close search' }))
    expect(props.onClose).toHaveBeenCalledTimes(1)
  })

  it('a new focus signal (⌘F while open) refocuses and selects the query', () => {
    const { input, rerender, props } = renderBar()
    fireEvent.change(input, { target: { value: 'widget' } })
    ;(input as HTMLInputElement).blur()
    rerender(<TranscriptSearchBar {...props} focusSignal={1} />)
    expect(input).toHaveFocus()
    const el = input as HTMLInputElement
    expect([el.selectionStart, el.selectionEnd]).toEqual([0, 6])
  })
})
