import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import type { FileDiff, SessionDiff } from '../diff/types'

const { diffHook } = vi.hoisted(() => ({ diffHook: vi.fn() }))
vi.mock('@/api/hooks', () => ({ useSessionDiff: diffHook }))

import { ChangesCard } from './ChangesCard'

const file = (i: number, over: Partial<FileDiff> = {}): FileDiff => ({
  path: `src/f${i}.ts`, oldPath: null, status: 'modified', additions: i, deletions: 1,
  binary: false, tooLarge: false, hunks: [], ...over,
})

const diff = (files: FileDiff[]): SessionDiff => ({
  base: 'abc', head: 'def', files, truncated: false,
  additions: files.reduce((n, f) => n + f.additions, 0),
  deletions: files.reduce((n, f) => n + f.deletions, 0),
})

describe('ChangesCard', () => {
  beforeEach(() => diffHook.mockReset())

  it('is a labelled "Changes" section', () => {
    diffHook.mockReturnValue({ data: undefined })
    render(<ChangesCard sessionId="s1" busy={false} onOpenChanges={() => {}} />)
    expect(screen.getByRole('region', { name: 'Changes' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Changes' })).toBeInTheDocument()
  })

  it('reads the summary diff with the busy flag (the header chip query)', () => {
    diffHook.mockReturnValue({ data: undefined })
    render(<ChangesCard sessionId="s1" busy onOpenChanges={() => {}} />)
    expect(diffHook).toHaveBeenCalledWith('s1', { summary: true, busy: true })
  })

  it('shows the totals and at most five files', () => {
    const files = [1, 2, 3, 4, 5, 6, 7].map((i) => file(i))
    diffHook.mockReturnValue({ data: diff(files) })
    render(<ChangesCard sessionId="s1" busy={false} onOpenChanges={() => {}} />)
    expect(screen.getByTestId('changes-totals')).toHaveTextContent('+28 −7 · 7 files')
    const list = screen.getByRole('list', { name: 'Changed files' })
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(5)
    expect(rows[0]).toHaveTextContent('src/f1.ts')
    expect(rows[0]).toHaveTextContent('modified')
    expect(rows[0]).toHaveTextContent('+1 −1')
    expect(screen.queryByText('src/f6.ts')).toBeNull()
  })

  it('says "1 file" for a single file', () => {
    diffHook.mockReturnValue({ data: diff([file(3, { status: 'added' })]) })
    render(<ChangesCard sessionId="s1" busy={false} onOpenChanges={() => {}} />)
    expect(screen.getByTestId('changes-totals')).toHaveTextContent('+3 −1 · 1 file')
  })

  it('Open Changes calls onOpenChanges', () => {
    const onOpen = vi.fn()
    diffHook.mockReturnValue({ data: diff([file(1)]) })
    render(<ChangesCard sessionId="s1" busy={false} onOpenChanges={onOpen} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open Changes' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('while the first fetch is pending, does not claim "No changes yet"', () => {
    diffHook.mockReturnValue({ data: undefined, isPending: true, isError: false })
    render(<ChangesCard sessionId="s1" busy={false} onOpenChanges={() => {}} />)
    expect(screen.queryByText('No changes yet')).toBeNull()
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open Changes' })).toBeNull()
  })

  it('a 404 (error, no data) shows "No changes yet"', () => {
    diffHook.mockReturnValue({ data: undefined, isPending: false, isError: true })
    render(<ChangesCard sessionId="s1" busy={false} onOpenChanges={() => {}} />)
    expect(screen.getByText('No changes yet')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open Changes' })).toBeNull()
  })

  it('an empty diff shows "No changes yet"', () => {
    diffHook.mockReturnValue({ data: diff([]), isPending: false, isError: false })
    render(<ChangesCard sessionId="s1" busy={false} onOpenChanges={() => {}} />)
    expect(screen.getByText('No changes yet')).toBeInTheDocument()
  })
})
