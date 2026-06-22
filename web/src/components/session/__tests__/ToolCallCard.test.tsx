import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ToolCallCard } from '../ToolCallCard'
import type { Block } from '@/session/reducer'

type ToolBlock = Extract<Block, { kind: 'tool' }>

const bashBlock: ToolBlock = {
  kind: 'tool',
  id: 'tool-1',
  toolId: 'tool-1',
  name: 'Bash',
  toolKind: 'bash',
  input: { command: 'npm test' },
}

const editBlock: ToolBlock = {
  kind: 'tool',
  id: 'tool-2',
  toolId: 'tool-2',
  name: 'Edit',
  toolKind: 'edit',
  input: { file_path: '/src/foo.ts' },
}

const bashBlockWithResult: ToolBlock = {
  ...bashBlock,
  result: { ok: true, payload: { content: 'test passed\nline 2\nline 3' } },
}

const bashBlockWithError: ToolBlock = {
  ...bashBlock,
  result: { ok: false, payload: { content: 'command not found' } },
}

const editBlockWithResult: ToolBlock = {
  ...editBlock,
  result: {
    ok: true,
    payload: {
      old_string: 'foo\nbar',
      new_string: 'baz\nqux\nextra',
    },
  },
}

describe('ToolCallCard', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('shows tool name', () => {
    render(<ToolCallCard block={bashBlock} density="card" />)
    expect(screen.getByText('Bash')).toBeInTheDocument()
  })

  it('shows key arg for bash (command)', () => {
    render(<ToolCallCard block={bashBlock} density="card" />)
    expect(screen.getByText('npm test')).toBeInTheDocument()
  })

  it('shows key arg for edit (file_path)', () => {
    render(<ToolCallCard block={editBlock} density="card" />)
    expect(screen.getByText('/src/foo.ts')).toBeInTheDocument()
  })

  it('is collapsed by default', () => {
    render(<ToolCallCard block={bashBlock} density="card" />)
    const toggle = screen.getByRole('button', { name: /args/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('expands to reveal input JSON', async () => {
    const user = userEvent.setup()
    render(<ToolCallCard block={bashBlock} density="card" />)
    const argsToggle = screen.getByRole('button', { name: /args/i })
    await user.click(argsToggle)
    const regionId = argsToggle.getAttribute('aria-controls')!
    expect(document.getElementById(regionId)!.hidden).toBe(false)
  })

  describe('result collapsing', () => {
    it('result is hidden by default when block.result is present', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="card" />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      const regionId = resultToggle.getAttribute('aria-controls')!
      expect(document.getElementById(regionId)!.hidden).toBe(true)
    })

    it('result toggle has aria-expanded=false by default', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="card" />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      expect(resultToggle).toHaveAttribute('aria-expanded', 'false')
    })

    it('clicking result toggle reveals the result content', async () => {
      const user = userEvent.setup()
      render(<ToolCallCard block={bashBlockWithResult} density="card" />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      await user.click(resultToggle)
      const regionId = resultToggle.getAttribute('aria-controls')!
      expect(document.getElementById(regionId)!.hidden).toBe(false)
    })

    it('clicking result toggle again collapses it', async () => {
      const user = userEvent.setup()
      render(<ToolCallCard block={bashBlockWithResult} density="card" />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      await user.click(resultToggle)
      await user.click(resultToggle)
      const regionId = resultToggle.getAttribute('aria-controls')!
      expect(document.getElementById(regionId)!.hidden).toBe(true)
    })

    it('shows ✓ glyph in collapsed summary for ok=true result', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="card" />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      // The toggle area should contain a success glyph
      expect(resultToggle.textContent).toContain('✓')
    })

    it('shows ✕ glyph in collapsed summary for ok=false result', () => {
      render(<ToolCallCard block={bashBlockWithError} density="card" />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      expect(resultToggle.textContent).toContain('✕')
    })

    it('shows line-count size hint for bash result', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="card" />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      // 3 lines in content: 'test passed\nline 2\nline 3'
      expect(resultToggle.textContent).toMatch(/3\s*lines?/)
    })

    it('shows "0 lines" for empty bash output, not "1 lines"', () => {
      const emptyOutputBlock: ToolBlock = {
        ...bashBlock,
        result: { ok: true, payload: { content: '' } },
      }
      render(<ToolCallCard block={emptyOutputBlock} density="card" />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      expect(resultToggle.textContent).toContain('0 lines')
      expect(resultToggle.textContent).not.toContain('1 line')
    })

    it('shows diff stat size hint for edit result', () => {
      render(<ToolCallCard block={editBlockWithResult} density="card" />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      // old_string has 2 lines (-2), new_string has 3 lines (+3)
      expect(resultToggle.textContent).toMatch(/[+-]\d+/)
    })

    it('defaultResultOpen=true auto-expands the result', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="card" defaultResultOpen />)
      const resultToggle = screen.getByRole('button', { name: /result/i })
      expect(resultToggle).toHaveAttribute('aria-expanded', 'true')
      const regionId = resultToggle.getAttribute('aria-controls')!
      expect(document.getElementById(regionId)!.hidden).toBe(false)
    })

    it('does not render a result toggle when block.result is absent', () => {
      render(<ToolCallCard block={bashBlock} density="card" />)
      expect(screen.queryByRole('button', { name: /result/i })).toBeNull()
    })
  })

  it('renders ToolResult when block.result is present and result is expanded', async () => {
    const user = userEvent.setup()
    const withResult: ToolBlock = {
      ...bashBlock,
      result: { ok: true, payload: { content: 'test passed' } },
    }
    render(<ToolCallCard block={withResult} density="card" />)
    // Result is collapsed by default — expand it
    await user.click(screen.getByRole('button', { name: /result/i }))
    expect(screen.getByText(/test passed/)).toBeInTheDocument()
  })

  describe('line density', () => {
    it('renders the tool name and key arg in a single row', () => {
      render(<ToolCallCard block={bashBlock} density="line" />)
      expect(screen.getByText('Bash')).toBeInTheDocument()
      expect(screen.getByText('npm test')).toBeInTheDocument()
    })

    it('is collapsed by default (detail hidden)', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="line" />)
      const toggle = screen.getByRole('button', { name: /tool details/i })
      expect(toggle).toHaveAttribute('aria-expanded', 'false')
      const regionId = toggle.getAttribute('aria-controls')!
      expect(document.getElementById(regionId)!.hidden).toBe(true)
    })

    it('clicking the row reveals input JSON and result', async () => {
      const user = userEvent.setup()
      render(<ToolCallCard block={bashBlockWithResult} density="line" />)
      const toggle = screen.getByRole('button', { name: /tool details/i })
      await user.click(toggle)
      const regionId = toggle.getAttribute('aria-controls')!
      expect(document.getElementById(regionId)!.hidden).toBe(false)
      expect(screen.getByText(/test passed/)).toBeInTheDocument()
    })

    it('shows ✓ and a size hint in the row when result is ok', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="line" />)
      const toggle = screen.getByRole('button', { name: /tool details/i })
      expect(toggle.textContent).toContain('✓')
      expect(toggle.textContent).toMatch(/3\s*lines?/)
    })

    it('shows ✕ when result failed', () => {
      render(<ToolCallCard block={bashBlockWithError} density="line" />)
      const toggle = screen.getByRole('button', { name: /tool details/i })
      expect(toggle.textContent).toContain('✕')
    })

    it('defaultResultOpen opens the detail panel', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="line" defaultResultOpen />)
      const toggle = screen.getByRole('button', { name: /tool details/i })
      expect(toggle).toHaveAttribute('aria-expanded', 'true')
    })

    it('toolDetailMode="all-open" force-opens the detail panel', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="line" toolDetailMode="all-open" />)
      expect(screen.getByRole('button', { name: /tool details/i })).toHaveAttribute('aria-expanded', 'true')
    })

    it('toolDetailMode="all-closed" overrides defaultResultOpen and keeps the panel shut', () => {
      render(<ToolCallCard block={bashBlockWithResult} density="line" defaultResultOpen toolDetailMode="all-closed" />)
      expect(screen.getByRole('button', { name: /tool details/i })).toHaveAttribute('aria-expanded', 'false')
    })

    it('dims a completed tool name and keeps a running one prominent', () => {
      const { rerender } = render(<ToolCallCard block={bashBlockWithResult} density="line" />)
      // Completed → muted name color
      expect(screen.getByText('Bash').className).toMatch(/text-gray-(500|600)/)
      rerender(<ToolCallCard block={bashBlock} density="line" />)
      // Running (no result) → prominent name color
      expect(screen.getByText('Bash').className).toMatch(/text-gray-(900|100)/)
    })
  })
})
