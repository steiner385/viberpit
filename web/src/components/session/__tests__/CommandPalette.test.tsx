// web/src/components/session/__tests__/CommandPalette.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { CommandPalette, type CommandPaletteHandle } from '@/components/session/CommandPalette'
import type { Command } from '@/session/events'

const CMDS: Command[] = [
  { name: 'model', description: 'Switch model', argumentHint: '<model>', kind: 'control' },
  { name: 'help', description: 'List commands', argumentHint: null, kind: 'client' },
  { name: 'review', description: 'Review a PR', argumentHint: '<pr#>', kind: 'passthrough' },
]

describe('CommandPalette', () => {
  it('filters by query and groups custom commands', () => {
    render(<CommandPalette commands={CMDS} query="" onSelect={vi.fn()} onComplete={vi.fn()} onDismiss={vi.fn()} />)
    expect(screen.getByText('model')).toBeInTheDocument()
    expect(screen.getByText('Custom')).toBeInTheDocument() // group divider for passthrough
  })

  it('Enter selects the highlighted command', () => {
    const onSelect = vi.fn()
    const ref = createRef<CommandPaletteHandle>()
    render(<CommandPalette ref={ref} commands={CMDS} query="mod" onSelect={onSelect} onComplete={vi.fn()} onDismiss={vi.fn()} />)
    const handled = ref.current!.handleKeyDown({ key: 'Enter', preventDefault() {} } as unknown as React.KeyboardEvent)
    expect(handled).toBe(true)
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ name: 'model' }))
  })

  it('renders nothing when no match', () => {
    const { container } = render(<CommandPalette commands={CMDS} query="zzz" onSelect={vi.fn()} onComplete={vi.fn()} onDismiss={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })
})
