import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ToolGroup } from '../ToolGroup'
import type { ToolBlock } from '@/session/groupBlocks'

function tool(id: string, withResult = true): ToolBlock {
  const base: ToolBlock = { kind: 'tool', id, toolId: id, name: 'Read', toolKind: 'read', input: { file_path: `/f/${id}` } }
  return withResult ? { ...base, result: { ok: true, payload: { content: 'x' } } } : base
}

const fourDone = [tool('a'), tool('b'), tool('c'), tool('d')]

describe('ToolGroup', () => {
  it('folds a completed run of >= threshold tools into a summary (auto, not live)', () => {
    render(<ToolGroup tools={fourDone} isLiveTail={false} toolDetailMode="auto" />)
    const header = screen.getByRole('button', { name: /tool group/i })
    expect(header.textContent).toMatch(/4 tool calls/)
    expect(header).toHaveAttribute('aria-expanded', 'false')
    const regionId = header.getAttribute('aria-controls')!
    expect(document.getElementById(regionId)!.hidden).toBe(true)
  })

  it('clicking the summary expands the rows', async () => {
    const user = userEvent.setup()
    render(<ToolGroup tools={fourDone} isLiveTail={false} toolDetailMode="auto" />)
    const header = screen.getByRole('button', { name: /tool group/i })
    await user.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('does not fold a short run (< threshold) in auto mode', () => {
    render(<ToolGroup tools={[tool('a'), tool('b'), tool('c')]} isLiveTail={false} toolDetailMode="auto" />)
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(3)
  })

  it('does not fold the live tail even when long', () => {
    render(<ToolGroup tools={fourDone} isLiveTail toolDetailMode="auto" />)
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('does not fold when a tool is still incomplete', () => {
    const withPending = [tool('a'), tool('b'), tool('c'), tool('d', false)]
    render(<ToolGroup tools={withPending} isLiveTail={false} toolDetailMode="auto" />)
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
  })

  it('all-closed folds even a short run', () => {
    render(<ToolGroup tools={[tool('a'), tool('b')]} isLiveTail={false} toolDetailMode="all-closed" />)
    const header = screen.getByRole('button', { name: /tool group/i })
    expect(header).toHaveAttribute('aria-expanded', 'false')
  })

  it('all-open shows rows with no summary header', () => {
    render(<ToolGroup tools={fourDone} isLiveTail={false} toolDetailMode="all-open" />)
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('marks the summary with ✕ when any tool failed', () => {
    const failed: ToolBlock = { ...tool('e'), result: { ok: false, payload: { content: 'boom' } } }
    render(<ToolGroup tools={[tool('a'), tool('b'), tool('c'), failed]} isLiveTail={false} toolDetailMode="auto" />)
    const header = screen.getByRole('button', { name: /tool group/i })
    expect(header.textContent).toContain('✕')
  })
})
