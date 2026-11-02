/**
 * D-5 line comments on the shared diff renderer (`CommentableDiff` over the
 * renderer's decoration slots): the gutter, range selection, the editor, saved
 * comments and stale anchors. Without decorations the renderer must stay exactly
 * the D-4 one (no gutter) — the transcript's inline Edit/Write diffs use it that way.
 */
import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DiffRenderer, type DiffViewMode } from './DiffRenderer'
import CommentableDiff from '../review/CommentableDiff'
import type { CommentAnchor, LineComment } from '../review/reviewComments'
import type { DiffLine, FileDiff } from './types'

const ctx = (oldNo: number, newNo: number, text: string): DiffLine => ({ kind: 'context', oldNo, newNo, text })
const del = (oldNo: number, text: string): DiffLine => ({ kind: 'del', oldNo, newNo: null, text })
const add = (newNo: number, text: string): DiffLine => ({ kind: 'add', oldNo: null, newNo, text })

const FILE: FileDiff = {
  path: 'src/app.ts', oldPath: null, status: 'modified', additions: 3, deletions: 1, binary: false, tooLarge: false,
  hunks: [
    { oldStart: 1, oldLines: 3, newStart: 1, newLines: 4, header: '', lines: [
      ctx(1, 1, 'one'), del(2, 'two'), add(2, 'TWO'), add(3, 'THREE'), ctx(3, 4, 'four'),
    ] },
    { oldStart: 20, oldLines: 2, newStart: 21, newLines: 3, header: '', lines: [
      ctx(20, 21, 'twenty-one'), add(22, 'twenty-two'), ctx(21, 23, 'twenty-three'),
    ] },
  ],
}

type Added = { anchor: CommentAnchor; quote: string[]; body: string }

/** The renderer wired to a local comment list, like ChangesTab wires the draft. */
function Harness({ file = FILE, view = 'unified', initial = [], onAdd, maxLines }: {
  file?: FileDiff; view?: DiffViewMode; initial?: LineComment[]; onAdd?: (a: Added) => void; maxLines?: number
}) {
  const [comments, setComments] = useState<LineComment[]>(initial)
  return (
    <CommentableDiff
      file={file}
      view={view}
      maxLines={maxLines}
      comments={comments}
      onAddComment={(anchor, quote, body) => {
        onAdd?.({ anchor, quote, body })
        setComments((p) => [...p, { id: `c${p.length + 1}`, anchor, quote, body }])
      }}
      onEditComment={(id, body) => setComments((p) => p.map((c) => (c.id === id ? { ...c, body } : c)))}
      onDeleteComment={(id) => setComments((p) => p.filter((c) => c.id !== id))}
    />
  )
}

const gutter = (n: number) => screen.findAllByRole('button', { name: `Comment on line ${n}` })

describe('DiffRenderer comments: gutter and editor', () => {
  it('clicking the gutter opens the editor; Ctrl+Enter saves and the comment renders inline', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    render(<Harness onAdd={onAdd} />)
    // Unified: line 3 exists once as a new-side number (the add 'THREE').
    await user.click((await gutter(3))[0])
    const box = await screen.findByRole('textbox', { name: /comment/i })
    await user.type(box, 'Rename this{Control>}{Enter}{/Control}')
    expect(onAdd).toHaveBeenCalledWith({
      anchor: { path: 'src/app.ts', side: 'new', start: 3, end: 3 },
      quote: ['THREE'],
      body: 'Rename this',
    })
    expect(screen.queryByRole('textbox', { name: /comment/i })).not.toBeInTheDocument()
    const card = await screen.findByRole('article', { name: /comment on line 3/i })
    expect(card).toHaveTextContent('Rename this')
  })

  it('an empty body cannot be saved, and Esc cancels', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    render(<Harness onAdd={onAdd} />)
    await user.click((await gutter(4))[0])
    const box = await screen.findByRole('textbox', { name: /comment/i })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    await user.type(box, '   {Control>}{Enter}{/Control}')
    expect(onAdd).not.toHaveBeenCalled()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('textbox', { name: /comment/i })).not.toBeInTheDocument()
  })

  it('Edit and Delete work on a saved comment', async () => {
    const user = userEvent.setup()
    const initial: LineComment[] = [{
      id: 'x', anchor: { path: 'src/app.ts', side: 'new', start: 2, end: 2 }, quote: ['TWO'], body: 'first',
    }]
    render(<Harness initial={initial} />)
    const card = await screen.findByRole('article', { name: /comment on line 2/i })
    await user.click(within(card).getByRole('button', { name: 'Edit' }))
    const box = within(card).getByRole('textbox', { name: /comment/i })
    expect(box).toHaveValue('first')
    await user.clear(box)
    await user.type(box, 'second')
    await user.click(within(card).getByRole('button', { name: 'Save' }))
    expect(screen.getByRole('article', { name: /comment on line 2/i })).toHaveTextContent('second')
    await user.click(within(screen.getByRole('article', { name: /comment on line 2/i })).getByRole('button', { name: 'Delete' }))
    expect(screen.queryByRole('article')).not.toBeInTheDocument()
  })
})

describe('DiffRenderer comments: ranges', () => {
  it('shift-click on the same side within the same hunk extends the range', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    render(<Harness onAdd={onAdd} />)
    await user.click((await gutter(2))[0])
    await user.keyboard('{Shift>}')
    await user.click((await gutter(4))[0])
    await user.keyboard('{/Shift}')
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'range{Control>}{Enter}{/Control}')
    expect(onAdd).toHaveBeenCalledWith({
      anchor: { path: 'src/app.ts', side: 'new', start: 2, end: 4 },
      quote: ['TWO', 'THREE', 'four'],
      body: 'range',
    })
  })

  it('shift-click in another hunk starts a new range', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    render(<Harness onAdd={onAdd} />)
    await user.click((await gutter(3))[0])
    await user.keyboard('{Shift>}')
    await user.click((await gutter(22))[0])
    await user.keyboard('{/Shift}')
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'x{Control>}{Enter}{/Control}')
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({
      anchor: { path: 'src/app.ts', side: 'new', start: 22, end: 22 },
    }))
  })

  it('shift-click on the other side starts a new range', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    render(<Harness view="split" onAdd={onAdd} />)
    const cells = (side: 'old' | 'new') => Array.from(document.querySelectorAll<HTMLElement>(`[data-side="${side}"]`))
    await gutter(1)
    const oldTwo = cells('old').find((c) => c.textContent?.includes('two'))!
    const newThree = cells('new').find((c) => c.textContent?.includes('THREE'))!
    await user.click(within(oldTwo).getByRole('button', { name: 'Comment on removed line 2' }))
    await user.keyboard('{Shift>}')
    await user.click(within(newThree).getByRole('button', { name: 'Comment on line 3' }))
    await user.keyboard('{/Shift}')
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'x{Control>}{Enter}{/Control}')
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({
      anchor: { path: 'src/app.ts', side: 'new', start: 3, end: 3 },
    }))
  })
})

describe('DiffRenderer comments: sides', () => {
  it('a comment on a removed line anchors side old (unified)', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    render(<Harness onAdd={onAdd} />)
    await gutter(1)
    const delRow = document.querySelector<HTMLElement>('[data-diff-row][data-kind="del"]')!
    await user.click(within(delRow).getByRole('button', { name: 'Comment on removed line 2' }))
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'why{Control>}{Enter}{/Control}')
    expect(onAdd).toHaveBeenCalledWith({
      anchor: { path: 'src/app.ts', side: 'old', start: 2, end: 2 }, quote: ['two'], body: 'why',
    })
  })

  it('split: the left column anchors old, even on a context line', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    render(<Harness view="split" onAdd={onAdd} />)
    await gutter(1)
    const left = Array.from(document.querySelectorAll<HTMLElement>('[data-side="old"]'))
      .find((c) => c.textContent?.includes('twenty-three'))!
    await user.click(within(left).getByRole('button', { name: 'Comment on removed line 21' }))
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'ctx{Control>}{Enter}{/Control}')
    expect(onAdd).toHaveBeenCalledWith({
      anchor: { path: 'src/app.ts', side: 'old', start: 21, end: 21 }, quote: ['twenty-three'], body: 'ctx',
    })
  })
})

describe('DiffRenderer comments: placement', () => {
  it('split: a comment on a context line renders once, under its row', async () => {
    const initial: LineComment[] = [{
      id: 'x', anchor: { path: 'src/app.ts', side: 'new', start: 4, end: 4 }, quote: ['four'], body: 'once',
    }]
    render(<Harness view="split" initial={initial} />)
    expect(await screen.findAllByRole('article')).toHaveLength(1)
  })
})

describe('DiffRenderer comments: gutter labels', () => {
  it('split view names the side: new column "Comment on line N", old column "Comment on removed line N"', () => {
    render(<Harness view="split" />)
    const cells = (side: 'old' | 'new') => Array.from(document.querySelectorAll<HTMLElement>(`[data-side="${side}"]`))
    const labels = (side: 'old' | 'new') => cells(side).flatMap((c) => within(c).queryAllByRole('button').map((b) => b.getAttribute('aria-label')))
    expect(labels('new')).toContain('Comment on line 3')
    expect(labels('new').every((l) => /^Comment on line \d+$/.test(l ?? ''))).toBe(true)
    expect(labels('old')).toContain('Comment on removed line 2')
    expect(labels('old').every((l) => /^Comment on removed line \d+$/.test(l ?? ''))).toBe(true)
  })
})

describe('DiffRenderer comments: a refetch while the editor is open', () => {
  it('keeps the editor and its text, and Save stores the snapshot quote', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    const { rerender } = render(<Harness onAdd={onAdd} />)
    await user.click((await gutter(22))[0])
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'half typed')
    // Refetch: the second hunk is gone and the first grew — line 22 no longer reads 'twenty-two'.
    const merged: FileDiff = { ...FILE, hunks: [{ ...FILE.hunks[0], lines: [
      ctx(1, 1, 'one'), add(2, 'TWO'), add(3, 'THREE'), ctx(3, 4, 'four'),
    ] }] }
    rerender(<Harness file={merged} onAdd={onAdd} />)
    const box = await screen.findByRole('textbox', { name: /comment/i })
    expect(box).toHaveValue('half typed')
    // It moved to the top of the file, marked outdated, before the first row.
    expect(box.closest('[data-comment-slot]')!.textContent).toMatch(/outdated/i)
    await user.type(box, ' done')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(onAdd).toHaveBeenCalledWith({
      anchor: { path: 'src/app.ts', side: 'new', start: 22, end: 22 }, quote: ['twenty-two'], body: 'half typed done',
    })
  })

  it('stays in place, text kept, when the refetch shifts hunks but the line still reads the same', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    const { rerender } = render(<Harness onAdd={onAdd} />)
    await user.click((await gutter(3))[0])
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'keep me')
    // A new hunk lands in front: hunk indices shift, line 3 is unchanged.
    const shifted: FileDiff = { ...FILE, hunks: [
      { oldStart: 0, oldLines: 0, newStart: 0, newLines: 0, header: '', lines: [] }, ...FILE.hunks,
    ] }
    rerender(<Harness file={shifted} onAdd={onAdd} />)
    const box = await screen.findByRole('textbox', { name: /comment/i })
    expect(box).toHaveValue('keep me')
    expect(box.closest('[data-comment-slot]')!.textContent).not.toMatch(/outdated/i)
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({ quote: ['THREE'], body: 'keep me' }))
  })
})

describe('DiffRenderer comments: stale anchors', () => {
  it('a comment whose line moved renders as a stale card at the top of the file', async () => {
    const moved: FileDiff = { ...FILE, hunks: [{ ...FILE.hunks[0], lines: [
      ctx(1, 1, 'one'), add(2, 'inserted'), del(2, 'two'), add(3, 'TWO'), add(4, 'THREE'), ctx(3, 5, 'four'),
    ] }] }
    const initial: LineComment[] = [{
      id: 'x', anchor: { path: 'src/app.ts', side: 'new', start: 3, end: 3 }, quote: ['THREE'], body: 'still here',
    }]
    const { container } = render(<Harness file={moved} initial={initial} />)
    const card = await screen.findByRole('article', { name: /comment on line 3/i })
    expect(card).toHaveTextContent(/outdated/i)
    expect(card).toHaveTextContent('still here')
    // At the top of the file: before the first diff row.
    const firstRow = container.querySelector('[data-diff-row]')!
    expect(card.compareDocumentPosition(firstRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

const comment = (id: string, n: number, quote: string, body: string): LineComment => ({
  id, anchor: { path: 'src/app.ts', side: 'new', start: n, end: n }, quote: [quote], body,
})

describe('DiffRenderer comments: focus returns', () => {
  it('Save of a new comment returns focus to the range\'s gutter button', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const btn = (await gutter(3))[0]
    await user.click(btn)
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'x{Control>}{Enter}{/Control}')
    expect(screen.getByRole('button', { name: 'Comment on line 3' })).toHaveFocus()
  })

  it('Cancel (Esc) of a new comment returns focus to the range\'s gutter button', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click((await gutter(2))[0])
    await user.keyboard('{Shift>}')
    await user.click((await gutter(4))[0])
    await user.keyboard('{/Shift}')
    await screen.findByRole('textbox', { name: /comment/i })
    await user.keyboard('{Escape}')
    expect(screen.getByRole('button', { name: 'Comment on line 4' })).toHaveFocus()
  })

  it('Save and Cancel of an edit return focus to that card\'s Edit button', async () => {
    const user = userEvent.setup()
    render(<Harness initial={[comment('x', 2, 'TWO', 'first')]} />)
    const card = await screen.findByRole('article', { name: /comment on line 2/i })
    await user.click(within(card).getByRole('button', { name: 'Edit' }))
    await user.type(within(card).getByRole('textbox', { name: /comment/i }), ' more')
    await user.click(within(card).getByRole('button', { name: 'Save' }))
    expect(within(card).getByRole('button', { name: 'Edit' })).toHaveFocus()
    await user.click(within(card).getByRole('button', { name: 'Edit' }))
    await user.click(within(card).getByRole('button', { name: 'Cancel' }))
    expect(within(card).getByRole('button', { name: 'Edit' })).toHaveFocus()
  })

  it('Delete focuses the next card\'s Edit button, and the last one\'s gutter button', async () => {
    const user = userEvent.setup()
    render(<Harness initial={[comment('a', 2, 'TWO', 'first'), comment('b', 3, 'THREE', 'second')]} />)
    const first = await screen.findByRole('article', { name: /comment on line 2/i })
    await user.click(within(first).getByRole('button', { name: 'Delete' }))
    const second = screen.getByRole('article', { name: /comment on line 3/i })
    expect(within(second).getByRole('button', { name: 'Edit' })).toHaveFocus()
    await user.click(within(second).getByRole('button', { name: 'Delete' }))
    expect(screen.queryByRole('article')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Comment on line 3' })).toHaveFocus()
  })
})

describe('DiffRenderer comments: clicking the open line again', () => {
  it('keeps the typed text (a later shift-extend remounts the editor with it)', async () => {
    const user = userEvent.setup()
    const onAdd = vi.fn()
    render(<Harness onAdd={onAdd} />)
    await user.click((await gutter(2))[0])
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'keep this')
    await user.click((await gutter(2))[0])
    await user.keyboard('{Shift>}')
    await user.click((await gutter(4))[0])
    await user.keyboard('{/Shift}')
    const box = await screen.findByRole('textbox', { name: /comment/i })
    expect(box).toHaveValue('keep this')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({
      anchor: { path: 'src/app.ts', side: 'new', start: 2, end: 4 }, body: 'keep this',
    }))
  })
})

describe('DiffRenderer comments: past the render cap', () => {
  it('after "show all", a comment past the cap renders inline and the editor opens in place', async () => {
    const user = userEvent.setup()
    // Cap 3 of the first hunk's 5 lines: line 22 (second hunk) is past it.
    render(<Harness maxLines={3} initial={[comment('far', 22, 'twenty-two', 'past the cap')]} />)
    const before = await screen.findByRole('article', { name: /comment on line 22/i })
    const firstRow = document.querySelector('[data-diff-row]')!
    // Capped: parked at the top of the file (not outdated — just not drawn).
    expect(before.compareDocumentPosition(firstRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(before).not.toHaveTextContent(/outdated/i)

    await user.click(screen.getByRole('button', { name: /show all \d+ lines/ }))
    const card = screen.getByRole('article', { name: /comment on line 22/i })
    expect(card).not.toHaveTextContent(/outdated/i)
    const row = screen.getByRole('button', { name: 'Comment on line 22' }).closest('[data-diff-row]')!
    expect(row.nextElementSibling).toContainElement(card)

    await user.click(screen.getByRole('button', { name: 'Comment on line 23' }))
    const box = await screen.findByRole('textbox', { name: /comment/i })
    const row23 = screen.getByRole('button', { name: 'Comment on line 23' }).closest('[data-diff-row]')!
    expect(row23.nextElementSibling).toContainElement(box)
    expect(box.closest('[data-comment-slot]')!.textContent).not.toMatch(/outdated/i)
  })
})

describe('DiffRenderer without decorations', () => {
  it('renders no gutter (inline transcript diffs)', () => {
    render(<DiffRenderer file={FILE} view="unified" />)
    expect(screen.queryAllByRole('button', { name: /comment on line/i })).toHaveLength(0)
    expect(document.querySelector('[data-comment-gutter]')).toBeNull()
  })
})
