import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import type { SearchHit, TranscriptSearchResult } from '@/api/transcript'

const fetchTranscriptSearch = vi.fn<(q: string) => Promise<TranscriptSearchResult>>()
const toastError = vi.hoisted(() => vi.fn())
vi.mock('sonner', () => ({ toast: { error: toastError } }))

import TranscriptSearchBar from './TranscriptSearchBar'

const hit = (n: number): SearchHit => ({
  turnId: `t${n}`, ordinal: n, role: 'assistant', snippet: `hit ${n}`, matchStart: 0, matchEnd: 3,
})
const result = (count: number, truncated = false): TranscriptSearchResult =>
  ({ hits: Array.from({ length: count }, (_, i) => hit(i)), truncated, totalTurns: 50 })

/** A promise the test resolves by hand, to order two responses. */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

function renderBar(over: Partial<React.ComponentProps<typeof TranscriptSearchBar>> = {}) {
  const props = {
    search: fetchTranscriptSearch, focusSignal: 0, onClose: vi.fn(), onJump: vi.fn(), onQuery: vi.fn(), ...over,
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

  it('reports the answered query to highlight once results arrive, and none when the query is cut short', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(1))
    const { input, props } = renderBar()
    await search(input, ' Foo  Bar ')
    expect(props.onQuery).toHaveBeenLastCalledWith('Foo  Bar')
    fireEvent.change(input, { target: { value: 'f' } })
    expect(props.onQuery).toHaveBeenLastCalledWith('')
  })

  it('Enter steps to the next hit and Shift+Enter to the previous, both wrapping', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(3))
    const { input, props } = renderBar()
    await search(input, 'hit')
    expect(screen.getByText('0 of 3')).toBeInTheDocument()

    const enter = (shiftKey = false) => fireEvent.keyDown(input, { key: 'Enter', shiftKey })
    enter()
    expect(props.onJump).toHaveBeenLastCalledWith(hit(0))
    expect(screen.getByText('1 of 3')).toBeInTheDocument()
    enter()
    enter()
    expect(props.onJump).toHaveBeenLastCalledWith(hit(2))
    enter() // wraps past the last
    expect(props.onJump).toHaveBeenLastCalledWith(hit(0))
    expect(screen.getByText('1 of 3')).toBeInTheDocument()
    enter(true) // wraps back past the first
    expect(props.onJump).toHaveBeenLastCalledWith(hit(2))
    expect(screen.getByText('3 of 3')).toBeInTheDocument()
  })

  it('Shift+Enter with no hit selected starts from the last (most recent) hit', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(3))
    const { input, props } = renderBar()
    await search(input, 'hit')
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(props.onJump).toHaveBeenLastCalledWith(hit(2))
  })

  it('the ↑ / ↓ buttons step like Shift+Enter / Enter', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(2))
    const { input, props } = renderBar()
    await search(input, 'hit')
    fireEvent.click(screen.getByRole('button', { name: 'Next match' }))
    expect(props.onJump).toHaveBeenLastCalledWith(hit(0))
    fireEvent.click(screen.getByRole('button', { name: 'Previous match' }))
    expect(props.onJump).toHaveBeenLastCalledWith(hit(1))
  })

  it('shows M+ when the results were truncated', async () => {
    fetchTranscriptSearch.mockResolvedValue(result(200, true))
    const { input } = renderBar()
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

  it('a failed search says so, and shows no stale count', async () => {
    fetchTranscriptSearch.mockRejectedValueOnce(new Error('boom'))
    const { input } = renderBar()
    await search(input, 'widget')
    expect(toastError).toHaveBeenCalledWith('Couldn’t search this conversation')
    expect(screen.queryByText(/ of /)).toBeNull()
  })

  it('discards a stale response that lands after a newer query\'s', async () => {
    const first = deferred<TranscriptSearchResult>()
    const second = deferred<TranscriptSearchResult>()
    fetchTranscriptSearch.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { input, props } = renderBar()
    await search(input, 'old')
    await search(input, 'new')
    await act(async () => { second.resolve(result(2)) })
    expect(screen.getByText('0 of 2')).toBeInTheDocument()
    await act(async () => { first.resolve(result(5)) })
    expect(screen.getByText('0 of 2')).toBeInTheDocument()
    expect(props.onQuery).toHaveBeenLastCalledWith('new')
  })

  it('discards a response for a query that has since been shortened below 2 characters', async () => {
    const pending = deferred<TranscriptSearchResult>()
    fetchTranscriptSearch.mockReturnValueOnce(pending.promise)
    const { input } = renderBar()
    await search(input, 'old')
    fireEvent.change(input, { target: { value: 'o' } })
    await act(async () => { pending.resolve(result(4)) })
    expect(screen.queryByText(/of 4/)).toBeNull()
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
