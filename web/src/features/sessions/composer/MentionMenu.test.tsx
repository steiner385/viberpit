// web/src/features/sessions/composer/MentionMenu.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { createRef } from 'react'
import { MentionMenu, type MentionMenuHandle, type MentionSource } from './MentionMenu'
import { mentionQueryAt } from '@/lib/mentionQuery'

const SESSIONS: MentionSource[] = [
  { id: 's1', title: 'Fix the login bug' },
  { id: 's2', title: 'Refactor the queue drainer' },
  { id: 's3', title: 'fixture cleanup' },
]

describe('MentionMenu', () => {
  it('filters by query (case-insensitive substring)', () => {
    render(<MentionMenu sessions={SESSIONS} query="fix" onSelect={vi.fn()} onDismiss={vi.fn()} />)
    expect(screen.getByText('Fix the login bug')).toBeInTheDocument()
    expect(screen.getByText('fixture cleanup')).toBeInTheDocument()
    expect(screen.queryByText('Refactor the queue drainer')).not.toBeInTheDocument()
  })

  it('renders nothing when no match', () => {
    const { container } = render(<MentionMenu sessions={SESSIONS} query="zzz" onSelect={vi.fn()} onDismiss={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('Enter selects the highlighted session', () => {
    const onSelect = vi.fn()
    const ref = createRef<MentionMenuHandle>()
    render(<MentionMenu ref={ref} sessions={SESSIONS} query="" onSelect={onSelect} onDismiss={vi.fn()} />)
    const handled = ref.current!.handleKeyDown({ key: 'Enter', preventDefault() {} } as unknown as React.KeyboardEvent)
    expect(handled).toBe(true)
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 's1' }))
  })

  it('ArrowDown/ArrowUp move the selection', () => {
    const onSelect = vi.fn()
    const ref = createRef<MentionMenuHandle>()
    render(<MentionMenu ref={ref} sessions={SESSIONS} query="" onSelect={onSelect} onDismiss={vi.fn()} />)
    act(() => { ref.current!.handleKeyDown({ key: 'ArrowDown', preventDefault() {} } as unknown as React.KeyboardEvent) })
    act(() => { ref.current!.handleKeyDown({ key: 'Enter', preventDefault() {} } as unknown as React.KeyboardEvent) })
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 's2' }))
  })

  it('Escape calls onDismiss even with zero matches', () => {
    const onDismiss = vi.fn()
    const ref = createRef<MentionMenuHandle>()
    render(<MentionMenu ref={ref} sessions={SESSIONS} query="zzz" onSelect={vi.fn()} onDismiss={onDismiss} />)
    const handled = ref.current!.handleKeyDown({ key: 'Escape', preventDefault() {} } as unknown as React.KeyboardEvent)
    expect(handled).toBe(true)
    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it('caps the listbox at 8 matches', () => {
    const many: MentionSource[] = Array.from({ length: 20 }, (_, i) => ({ id: `s${i}`, title: `session ${i}` }))
    render(<MentionMenu sessions={many} query="" onSelect={vi.fn()} onDismiss={vi.fn()} />)
    expect(screen.getAllByRole('option')).toHaveLength(8)
  })
})

describe('MentionMenu — Files group (D-7)', () => {
  const FILES = ['src/Composer.tsx', 'src/composer/util.ts']
  const key = (k: string) => ({ key: k, preventDefault() {} } as unknown as React.KeyboardEvent)

  it('lists the Files group above the Sessions group, each labelled', () => {
    render(<MentionMenu sessions={SESSIONS} files={FILES} query="" onSelect={vi.fn()} onSelectFile={vi.fn()} onDismiss={vi.fn()} />)
    const groups = screen.getAllByRole('group')
    expect(groups.map((g) => g.getAttribute('aria-label') ?? g.textContent)).toHaveLength(2)
    expect(screen.getByRole('group', { name: 'Files' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Sessions' })).toBeInTheDocument()
    const opts = screen.getAllByRole('option').map((o) => o.textContent)
    expect(opts.slice(0, 2)).toEqual(FILES)
    expect(opts.indexOf('src/Composer.tsx')).toBeLessThan(opts.indexOf('Fix the login bug'))
  })

  it('does not filter files client-side (the server already ranked them)', () => {
    render(<MentionMenu sessions={[]} files={FILES} query="comp" onSelect={vi.fn()} onSelectFile={vi.fn()} onDismiss={vi.fn()} />)
    expect(screen.getAllByRole('option')).toHaveLength(2)
  })

  it('Enter on a file calls onSelectFile with its path; Tab selects too', () => {
    const onSelectFile = vi.fn()
    const onSelect = vi.fn()
    const ref = createRef<MentionMenuHandle>()
    render(<MentionMenu ref={ref} sessions={SESSIONS} files={FILES} query="" onSelect={onSelect} onSelectFile={onSelectFile} onDismiss={vi.fn()} />)
    expect(ref.current!.handleKeyDown(key('Enter'))).toBe(true)
    expect(onSelectFile).toHaveBeenCalledWith('src/Composer.tsx')
    act(() => { ref.current!.handleKeyDown(key('ArrowDown')) })
    act(() => { ref.current!.handleKeyDown(key('ArrowDown')) })
    expect(ref.current!.handleKeyDown(key('Tab'))).toBe(true)
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 's1' }))
  })

  it('shows a loading row while the first file results are in flight', () => {
    render(<MentionMenu sessions={[]} files={undefined} filesLoading query="" onSelect={vi.fn()} onSelectFile={vi.fn()} onDismiss={vi.fn()} />)
    expect(screen.getByRole('group', { name: 'Files' })).toHaveTextContent(/searching files/i)
    // The loading row is a disabled option, never a pickable one.
    const [row] = screen.getAllByRole('option')
    expect(row).toHaveAttribute('aria-disabled', 'true')
    expect(row).toHaveTextContent(/searching files/i)
  })

  it('Enter/Tab while files load with nothing to pick are consumed, never passed on', () => {
    const onSelect = vi.fn()
    const onSelectFile = vi.fn()
    const ref = createRef<MentionMenuHandle>()
    render(<MentionMenu ref={ref} sessions={SESSIONS} files={undefined} filesLoading query="zzz" onSelect={onSelect} onSelectFile={onSelectFile} onDismiss={vi.fn()} />)
    expect(ref.current!.handleKeyDown(key('Enter'))).toBe(true)
    expect(ref.current!.handleKeyDown(key('Tab'))).toBe(true)
    expect(onSelect).not.toHaveBeenCalled()
    expect(onSelectFile).not.toHaveBeenCalled()
  })

  it('arriving file results keep a highlight the user moved', () => {
    const onSelect = vi.fn()
    const ref = createRef<MentionMenuHandle>()
    const props = { sessions: SESSIONS, query: '', onSelect, onSelectFile: vi.fn(), onDismiss: vi.fn() }
    const { rerender } = render(<MentionMenu ref={ref} {...props} files={['a.ts']} />)
    act(() => { ref.current!.handleKeyDown(key('ArrowDown')) }) // a.ts -> 'Fix the login bug'
    rerender(<MentionMenu ref={ref} {...props} files={['a.ts', 'b.ts', 'c.ts']} />)
    expect(screen.getByRole('option', { name: 'Fix the login bug' })).toHaveAttribute('aria-selected', 'true')
    ref.current!.handleKeyDown(key('Enter'))
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 's1' }))
  })

  it('a new query re-highlights the top option', () => {
    const ref = createRef<MentionMenuHandle>()
    const props = { sessions: SESSIONS, files: FILES, onSelect: vi.fn(), onSelectFile: vi.fn(), onDismiss: vi.fn() }
    const { rerender } = render(<MentionMenu ref={ref} {...props} query="" />)
    act(() => { ref.current!.handleKeyDown(key('ArrowDown')) })
    rerender(<MentionMenu ref={ref} {...props} query="f" />)
    expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('reports the active option id for aria-activedescendant', () => {
    const onActiveChange = vi.fn()
    const ref = createRef<MentionMenuHandle>()
    render(<MentionMenu ref={ref} sessions={SESSIONS} files={FILES} query="" onSelect={vi.fn()} onSelectFile={vi.fn()} onDismiss={vi.fn()} onActiveChange={onActiveChange} />)
    const [first, second] = screen.getAllByRole('option')
    expect(onActiveChange).toHaveBeenLastCalledWith(first.id)
    act(() => { ref.current!.handleKeyDown(key('ArrowDown')) })
    expect(onActiveChange).toHaveBeenLastCalledWith(second.id)
    expect(second).toHaveAttribute('aria-selected', 'true')
  })

  it('options meet the 44px coarse-pointer floor', () => {
    render(<MentionMenu sessions={SESSIONS} files={FILES} query="" onSelect={vi.fn()} onSelectFile={vi.fn()} onDismiss={vi.fn()} />)
    for (const o of screen.getAllByRole('option')) expect(o).toHaveClass('min-h-11')
  })

  it('a sessions-only menu (assistant drawer) has no Files group', () => {
    render(<MentionMenu sessions={SESSIONS} query="" onSelect={vi.fn()} onDismiss={vi.fn()} />)
    expect(screen.queryByRole('group', { name: 'Files' })).not.toBeInTheDocument()
    expect(screen.getByRole('listbox', { name: /session mentions/i })).toBeInTheDocument()
  })
})

describe('mentionQueryAt', () => {
  it('returns null inside an inline code span', () => {
    expect(mentionQueryAt('see `foo @comp', 14)).toBeNull()
  })

  it('opens again after a closed code span', () => {
    expect(mentionQueryAt('see `foo` @comp', 15)).toBe('comp')
  })

  it('returns null inside a fenced code block', () => {
    const t = '```\nconst a = @comp'
    expect(mentionQueryAt(t, t.length)).toBeNull()
  })

  it('opens after a closed fenced code block', () => {
    const t = '```\nx\n```\n@comp'
    expect(mentionQueryAt(t, t.length)).toBe('comp')
  })

  it('returns the text after the last @ up to the caret', () => {
    expect(mentionQueryAt('hey @fi', 7)).toBe('fi')
  })

  it('opens mid-message, not only at position 0', () => {
    expect(mentionQueryAt('check @bug please', 10)).toBe('bug')
  })

  it('returns null with no @ before the caret', () => {
    expect(mentionQueryAt('no mention here', 5)).toBeNull()
  })

  it('returns null once a space closes the mention', () => {
    expect(mentionQueryAt('hey @fix it', 9)).toBeNull()
  })

  it('returns null for an email-shaped @ (not preceded by whitespace or start)', () => {
    expect(mentionQueryAt('me@example', 10)).toBeNull()
  })

  it('returns empty string right after a bare @', () => {
    expect(mentionQueryAt('@', 1)).toBe('')
  })
})
