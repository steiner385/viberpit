import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DiffRenderer } from './DiffRenderer'
import { registeredLanguages } from './diffHighlight'
import { PATH_LANGUAGES, languageForPath } from './pathLanguages'
import type { DiffLine, FileDiff } from './types'

const ctx = (oldNo: number, newNo: number, text: string): DiffLine => ({ kind: 'context', oldNo, newNo, text })
const del = (oldNo: number, text: string): DiffLine => ({ kind: 'del', oldNo, newNo: null, text })
const add = (newNo: number, text: string): DiffLine => ({ kind: 'add', oldNo: null, newNo, text })

function file(path: string, lines: DiffLine[], over: Partial<FileDiff> = {}): FileDiff {
  return {
    path, oldPath: null, status: 'modified',
    additions: lines.filter((l) => l.kind === 'add').length,
    deletions: lines.filter((l) => l.kind === 'del').length,
    binary: false, tooLarge: false,
    hunks: [{ oldStart: 10, oldLines: 3, newStart: 10, newLines: 4, header: 'function f()', lines }],
    ...over,
  }
}

// Two dels followed by three adds: the canonical run the split view pairs.
const RUN = file('notes.unknownext', [
  ctx(10, 10, 'keep'),
  del(11, 'old one'),
  del(12, 'old two'),
  add(11, 'new one'),
  add(12, 'new two'),
  add(13, 'new three'),
  ctx(13, 14, 'tail'),
])

const rows = (c: HTMLElement) => Array.from(c.querySelectorAll<HTMLElement>('[data-diff-row]'))

describe('DiffRenderer: unified', () => {
  it('renders the hunk header from the hunk range and section text', () => {
    render(<DiffRenderer file={RUN} view="unified" />)
    expect(screen.getByText('@@ -10,3 +10,4 @@ function f()')).toBeInTheDocument()
  })

  it('shows old and new line numbers and marks add/del lines', () => {
    const { container } = render(<DiffRenderer file={RUN} view="unified" />)
    const r = rows(container)
    expect(r).toHaveLength(7)
    // context: both numbers
    expect(r[0]).toHaveAttribute('data-kind', 'context')
    expect(within(r[0]).getByText('10', { selector: '[data-old-no]' })).toBeInTheDocument()
    expect(within(r[0]).getByText('10', { selector: '[data-new-no]' })).toBeInTheDocument()
    // del: old number only, '-' gutter, del class
    expect(r[1]).toHaveAttribute('data-kind', 'del')
    expect(r[1]).toHaveClass('hljs-deletion')
    expect(r[1].querySelector('[data-old-no]')).toHaveTextContent('11')
    expect(r[1].querySelector('[data-new-no]')).toHaveTextContent('')
    expect(r[1].querySelector('[data-sign]')).toHaveTextContent('-')
    // add: new number only, '+' gutter, add class
    expect(r[3]).toHaveAttribute('data-kind', 'add')
    expect(r[3]).toHaveClass('hljs-addition')
    expect(r[3].querySelector('[data-old-no]')).toHaveTextContent('')
    expect(r[3].querySelector('[data-new-no]')).toHaveTextContent('11')
    expect(r[3].querySelector('[data-sign]')).toHaveTextContent('+')
    expect(r[6].querySelector('[data-new-no]')).toHaveTextContent('14')
  })

  it('omits line-number columns and the hunk header when no line is anchored in the file', () => {
    const unanchored = file('a.unknownext', [
      { kind: 'del', oldNo: null, newNo: null, text: 'x' },
      { kind: 'add', oldNo: null, newNo: null, text: 'y' },
    ])
    const { container } = render(<DiffRenderer file={unanchored} view="unified" />)
    expect(container.querySelector('[data-old-no]')).toBeNull()
    expect(screen.queryByText(/^@@/)).toBeNull()
  })

  it('says why a binary or too-large file has no hunks', () => {
    const { rerender } = render(<DiffRenderer file={file('a.png', [], { binary: true, hunks: [] })} view="unified" />)
    expect(screen.getByText(/binary file/i)).toBeInTheDocument()
    rerender(<DiffRenderer file={file('a.ts', [], { tooLarge: true, hunks: [] })} view="unified" />)
    expect(screen.getByText(/too large/i)).toBeInTheDocument()
  })
})

describe('DiffRenderer: split', () => {
  it('pairs a 2-del/3-add run row by row, leaving the unpaired add an empty left cell', () => {
    const { container } = render(<DiffRenderer file={RUN} view="split" />)
    const r = rows(container)
    // keep | 3 paired/unpaired rows | tail
    expect(r).toHaveLength(5)
    const cells = (row: HTMLElement) => [row.querySelector('[data-side="old"]')!, row.querySelector('[data-side="new"]')!]

    const [l0, r0] = cells(r[0])
    expect(l0).toHaveTextContent('keep')
    expect(r0).toHaveTextContent('keep')

    const [l1, r1] = cells(r[1])
    expect(l1).toHaveAttribute('data-kind', 'del')
    expect(l1).toHaveTextContent('old one')
    expect(r1).toHaveAttribute('data-kind', 'add')
    expect(r1).toHaveTextContent('new one')

    const [l2, r2] = cells(r[2])
    expect(l2).toHaveTextContent('old two')
    expect(r2).toHaveTextContent('new two')

    const [l3, r3] = cells(r[3])
    expect(l3).toHaveAttribute('data-kind', 'empty')
    expect(l3).toHaveTextContent('')
    expect(r3).toHaveTextContent('new three')

    const [l4, r4] = cells(r[4])
    expect(l4).toHaveTextContent('tail')
    expect(r4).toHaveTextContent('tail')
    expect(l4.querySelector('[data-old-no]')).toHaveTextContent('13')
    expect(r4.querySelector('[data-new-no]')).toHaveTextContent('14')
  })

  it('gives an unpaired del an empty right cell', () => {
    const f = file('a.unknownext', [del(1, 'gone'), ctx(2, 1, 'same')])
    const { container } = render(<DiffRenderer file={f} view="split" />)
    const first = rows(container)[0]
    expect(first.querySelector('[data-side="old"]')).toHaveTextContent('gone')
    expect(first.querySelector('[data-side="new"]')).toHaveAttribute('data-kind', 'empty')
  })
})

describe('DiffRenderer: syntax highlighting', () => {
  it('highlights a .ts file into hljs token spans', async () => {
    const f = file('src/a.ts', [add(1, 'const answer: number = 42')])
    const { container } = render(<DiffRenderer file={f} view="unified" />)
    // The highlighter is its own chunk — the first render is plain text.
    await screen.findByText('const', { selector: '.hljs-keyword' })
    expect(container.querySelector('[data-diff-row]')).toHaveTextContent('const answer: number = 42')
  })

  it('renders an unknown extension as plain text, with no token spans', async () => {
    const f = file('notes.unknownext', [add(1, 'const answer = 42')])
    const { container } = render(<DiffRenderer file={f} view="unified" />)
    // Give a (wrongly) loading highlighter every chance to land.
    await new Promise((r) => setTimeout(r, 50))
    expect(container.querySelector('[class^="hljs-"]:not([data-diff-row])')).toBeNull()
    expect(screen.getByText('const answer = 42')).toBeInTheDocument()
  })

  it('escapes source text rather than injecting it as markup', async () => {
    const f = file('a.ts', [add(1, 'const s = "<img src=x onerror=alert(1)>"')])
    const { container } = render(<DiffRenderer file={f} view="unified" />)
    await screen.findByText('const', { selector: '.hljs-keyword' })
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('[data-diff-row]')).toHaveTextContent('<img src=x onerror=alert(1)>')
  })

  it('maps extensions and well-known filenames to registered languages only', () => {
    expect(languageForPath('web/src/App.tsx')).toBe('typescript')
    expect(languageForPath('server/app.py')).toBe('python')
    expect(languageForPath('deploy/Dockerfile')).toBe('dockerfile')
    expect(languageForPath('README')).toBeNull()
    expect(languageForPath('x.unknownext')).toBeNull()
    // The map and the chunk's registrations are two halves of one contract.
    for (const lang of PATH_LANGUAGES) expect(registeredLanguages()).toContain(lang)
  })
})

describe('DiffRenderer: long diffs', () => {
  const long = file('a.unknownext', Array.from({ length: 300 }, (_, i) => add(i + 1, `line${i}`)))

  it('truncates at maxLines and offers to show all', () => {
    const { container } = render(<DiffRenderer file={long} view="unified" maxLines={200} />)
    expect(rows(container)).toHaveLength(200)
    expect(screen.getByRole('button', { name: /show all 300 lines/i })).toBeInTheDocument()
  })

  it('"show all" expands every line', async () => {
    const user = userEvent.setup()
    const { container } = render(<DiffRenderer file={long} view="unified" maxLines={200} />)
    await user.click(screen.getByRole('button', { name: /show all/i }))
    expect(rows(container)).toHaveLength(300)
    expect(screen.queryByRole('button', { name: /show all/i })).toBeNull()
  })

  it('renders everything with no button when maxLines is not set', () => {
    const { container } = render(<DiffRenderer file={long} view="unified" />)
    expect(rows(container)).toHaveLength(300)
    expect(screen.queryByRole('button', { name: /show all/i })).toBeNull()
  })
})
