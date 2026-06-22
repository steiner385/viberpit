import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ToolGroup } from '../ToolGroup'
import type { ToolBlock } from '@/session/groupBlocks'

function tool(id: string, withResult = true): ToolBlock {
  const base: ToolBlock = { kind: 'tool', id, toolId: id, name: 'Read', toolKind: 'read', input: { file_path: `/f/${id}` } }
  return withResult ? { ...base, result: { ok: true, payload: { content: 'x' } } } : base
}

const fourDone = [tool('a'), tool('b'), tool('c'), tool('d')]

/** Claude-Code-faithful: every tool is its own compact line — never a rollup count. */
describe('ToolGroup', () => {
  it('renders one row per tool with no rollup summary header (auto, not live)', () => {
    render(<ToolGroup tools={fourDone} isLiveTail={false} toolDetailMode="auto" />)
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('keeps every result collapsed in auto mode when not the live tail', () => {
    render(<ToolGroup tools={fourDone} isLiveTail={false} toolDetailMode="auto" />)
    for (const row of screen.getAllByRole('button', { name: /tool details/i })) {
      expect(row).toHaveAttribute('aria-expanded', 'false')
    }
  })

  it('auto-opens only the last tool of the live tail', () => {
    render(<ToolGroup tools={fourDone} isLiveTail toolDetailMode="auto" />)
    const rows = screen.getAllByRole('button', { name: /tool details/i })
    expect(rows.slice(0, 3).every((r) => r.getAttribute('aria-expanded') === 'false')).toBe(true)
    expect(rows[3]).toHaveAttribute('aria-expanded', 'true')
  })

  it('all-open expands every result panel', () => {
    render(<ToolGroup tools={fourDone} isLiveTail={false} toolDetailMode="all-open" />)
    for (const row of screen.getAllByRole('button', { name: /tool details/i })) {
      expect(row).toHaveAttribute('aria-expanded', 'true')
    }
  })

  it('all-closed collapses every result panel, even on the live tail', () => {
    render(<ToolGroup tools={fourDone} isLiveTail toolDetailMode="all-closed" />)
    for (const row of screen.getAllByRole('button', { name: /tool details/i })) {
      expect(row).toHaveAttribute('aria-expanded', 'false')
    }
  })

  it('renders a row for an incomplete tool too (no result yet)', () => {
    const withPending = [tool('a'), tool('b', false)]
    render(<ToolGroup tools={withPending} isLiveTail={false} toolDetailMode="auto" />)
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(2)
  })
})
