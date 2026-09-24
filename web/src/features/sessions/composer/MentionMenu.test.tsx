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

describe('mentionQueryAt', () => {
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
