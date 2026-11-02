import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ViewportProvider } from '@/shell/useViewport'
import { stubMatchMedia, WIDTH_TIER_QUERIES } from '@/test/matchMedia'
import { idleQueryResult, queryResult } from '@/test/reactQuery'
import { DIFF_VIEW_KEY } from './diffViewPreference'
import type { FileDiff, SessionDiff } from './types'

vi.mock('@/api/hooks', () => ({ useSessionDiff: vi.fn() }))

import { useSessionDiff } from '@/api/hooks'
import ChangesTab from './ChangesTab'

const file = (over: Partial<FileDiff> = {}): FileDiff => ({
  path: 'src/app.ts',
  oldPath: null,
  status: 'modified',
  additions: 2,
  deletions: 1,
  binary: false,
  tooLarge: false,
  hunks: [{
    oldStart: 1, oldLines: 4, newStart: 1, newLines: 5, header: '',
    lines: [
      { kind: 'context', oldNo: 1, newNo: 1, text: 'a' },
      { kind: 'del', oldNo: 2, newNo: null, text: 'b' },
      { kind: 'add', oldNo: null, newNo: 2, text: 'B' },
      { kind: 'context', oldNo: 3, newNo: 3, text: 'c' },
      { kind: 'context', oldNo: 4, newNo: 4, text: 'd' },
      { kind: 'add', oldNo: null, newNo: 5, text: 'e' },
    ],
  }],
  ...over,
})

const diff = (over: Partial<SessionDiff> = {}): SessionDiff => ({
  base: '0123456789abcdef',
  head: 'fedcba9876543210',
  files: [file()],
  additions: 2,
  deletions: 1,
  truncated: false,
  ...over,
})

function serve(d: SessionDiff) {
  vi.mocked(useSessionDiff).mockImplementation(() => queryResult(d))
}

function renderTab(tier: 'phone' | 'desktop' = 'desktop') {
  stubMatchMedia(WIDTH_TIER_QUERIES[tier])
  return render(
    <ViewportProvider kiosk={null}>
      <ChangesTab sessionId="s1" onSendReview={vi.fn()} />
    </ViewportProvider>,
  )
}

beforeEach(() => {
  vi.mocked(useSessionDiff).mockReset()
  serve(diff())
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ChangesTab — files', () => {
  it('lists each file with its counts and renders its diff', () => {
    renderTab()
    const header = screen.getByRole('button', { name: /src\/app\.ts/ })
    expect(header).toHaveAttribute('aria-expanded', 'true')
    expect(header).toHaveTextContent('+2')
    expect(header).toHaveTextContent('−1')
    expect(document.querySelectorAll('[data-diff-row]').length).toBeGreaterThan(0)
  })

  it('collapses a file', async () => {
    renderTab()
    await userEvent.click(screen.getByRole('button', { name: /src\/app\.ts/ }))
    expect(screen.getByRole('button', { name: /src\/app\.ts/ })).toHaveAttribute('aria-expanded', 'false')
    expect(document.querySelectorAll('[data-diff-row]')).toHaveLength(0)
  })

  it('shows a rename as old → new', () => {
    serve(diff({ files: [file({ status: 'renamed', oldPath: 'src/old.ts', path: 'src/new.ts' })] }))
    renderTab()
    expect(screen.getByRole('button', { name: /src\/old\.ts → src\/new\.ts/ })).toBeInTheDocument()
  })
})

describe('ChangesTab — view mode', () => {
  it('defaults to unified below 1024px', () => {
    renderTab('phone')
    expect(screen.getByRole('button', { name: 'Unified' })).toHaveAttribute('aria-pressed', 'true')
    expect(document.querySelector('[data-side]')).toBeNull()
  })

  it('defaults to split at 1024px and wider', () => {
    renderTab('desktop')
    expect(screen.getByRole('button', { name: 'Split' })).toHaveAttribute('aria-pressed', 'true')
    expect(document.querySelector('[data-side="new"]')).not.toBeNull()
  })

  it('remembers the choice per browser', async () => {
    const { unmount } = renderTab('desktop')
    await userEvent.click(screen.getByRole('button', { name: 'Unified' }))
    expect(localStorage.getItem(DIFF_VIEW_KEY)).toBe('unified')
    unmount()
    renderTab('desktop')
    expect(screen.getByRole('button', { name: 'Unified' })).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('ChangesTab — whitespace', () => {
  it('Hide whitespace refetches with ignoreWhitespace', async () => {
    renderTab()
    expect(vi.mocked(useSessionDiff)).toHaveBeenLastCalledWith('s1', { ignoreWhitespace: false })
    const toggle = screen.getByRole('button', { name: 'Hide whitespace' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    expect(vi.mocked(useSessionDiff)).toHaveBeenLastCalledWith('s1', { ignoreWhitespace: true })
  })
})

describe('ChangesTab — change navigation', () => {
  it('Next change focuses each change run in turn, Previous goes back', async () => {
    renderTab('phone')
    const next = screen.getByRole('button', { name: 'Next change' })
    const prev = screen.getByRole('button', { name: 'Previous change' })

    await userEvent.click(next)
    const first = document.activeElement as HTMLElement
    expect(first).toHaveAttribute('data-diff-row')
    expect(first).toHaveAttribute('data-kind', 'del')
    expect(first.scrollIntoView).toHaveBeenCalled()

    await userEvent.click(next)
    const second = document.activeElement as HTMLElement
    expect(second).toHaveAttribute('data-kind', 'add')
    expect(second).toHaveTextContent('e')

    await userEvent.click(prev)
    expect(document.activeElement).toBe(first)
  })

  it('finds runs in split view too', async () => {
    renderTab('desktop')
    await userEvent.click(screen.getByRole('button', { name: 'Next change' }))
    const row = document.activeElement as HTMLElement
    expect(row).toHaveAttribute('data-diff-row')
    expect(within(row).getByText('b')).toBeInTheDocument()
  })
})

describe('ChangesTab — notices', () => {
  it('names the base it compares against', () => {
    renderTab()
    expect(screen.getByText(/vs 0123456/)).toBeInTheDocument()
  })

  it('says uncommitted changes only when there is no base', () => {
    serve(diff({ base: null }))
    renderTab()
    expect(screen.getByText(/uncommitted changes only/i)).toBeInTheDocument()
  })

  it('warns when the diff was truncated', () => {
    serve(diff({ truncated: true, files: [file(), file({ path: 'big/later.ts', hunks: [] })] }))
    renderTab()
    expect(screen.getByRole('note')).toHaveTextContent(/truncated/i)
    expect(screen.getByText(/not loaded/i)).toBeInTheDocument()
  })

  it('shows the binary and too-large notices', () => {
    serve(diff({ files: [
      file({ path: 'img.png', binary: true, hunks: [] }),
      file({ path: 'huge.json', tooLarge: true, hunks: [] }),
    ] }))
    renderTab()
    expect(screen.getByText(/binary file/i)).toBeInTheDocument()
    expect(screen.getByText(/too large/i)).toBeInTheDocument()
  })

  it('shows an error with a retry', async () => {
    const refetch = vi.fn()
    vi.mocked(useSessionDiff).mockImplementation(() => idleQueryResult<SessionDiff>({
      isError: true, isPending: false, error: new Error('boom'), refetch,
    }))
    renderTab()
    await userEvent.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(refetch).toHaveBeenCalled())
  })
})
