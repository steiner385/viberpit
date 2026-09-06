// web/src/components/session/__tests__/CommandPalette.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { CommandPalette, type CommandPaletteHandle } from '@/components/session/CommandPalette'
import type { Command } from '@/session/events'

const CMDS: Command[] = [
  { name: 'model', description: 'Switch model', argumentHint: '<model>', kind: 'control', source: 'app' },
  { name: 'help', description: 'List commands', argumentHint: null, kind: 'client', source: 'app' },
  { name: 'review', description: 'Review a PR', argumentHint: '<pr#>', kind: 'passthrough', source: 'vendor' },
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

  // #1286: an engine whose catalog is skills (Codex) groups them under their own
  // heading. `skill` sorts with `passthrough` because both mean "the engine runs
  // this"; only the label differs, so the group is named for what it holds
  // rather than borrowing Claude's "Custom".
  it('groups an engine-published skill under a Skills heading', () => {
    const cmds: Command[] = [
      { name: 'model', description: 'Switch model', argumentHint: '<model>', kind: 'control', source: 'app' },
      { name: 'pr-review', description: 'Review a PR', argumentHint: null, kind: 'skill', source: 'vendor' },
    ]
    render(<CommandPalette commands={cmds} query="" onSelect={vi.fn()} onComplete={vi.fn()} onDismiss={vi.fn()} />)
    expect(screen.getByText('Skills')).toBeInTheDocument()
    expect(screen.queryByText('Custom')).not.toBeInTheDocument()
    // App-owned commands stay above the engine's own catalog.
    const names = screen.getAllByRole('option').map((o) => o.textContent)
    expect(names[0]).toContain('model')
    expect(names[1]).toContain('pr-review')
  })
})
