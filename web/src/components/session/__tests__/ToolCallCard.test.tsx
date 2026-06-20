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

describe('ToolCallCard', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('shows tool name', () => {
    render(<ToolCallCard block={bashBlock} />)
    expect(screen.getByText('Bash')).toBeInTheDocument()
  })

  it('shows key arg for bash (command)', () => {
    render(<ToolCallCard block={bashBlock} />)
    expect(screen.getByText('npm test')).toBeInTheDocument()
  })

  it('shows key arg for edit (file_path)', () => {
    render(<ToolCallCard block={editBlock} />)
    expect(screen.getByText('/src/foo.ts')).toBeInTheDocument()
  })

  it('is collapsed by default', () => {
    render(<ToolCallCard block={bashBlock} />)
    const toggle = screen.getByRole('button')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('expands to reveal input JSON', async () => {
    const user = userEvent.setup()
    render(<ToolCallCard block={bashBlock} />)
    await user.click(screen.getByRole('button'))
    // After expansion the input JSON pre should be visible (not hidden)
    const regionId = screen.getByRole('button').getAttribute('aria-controls')!
    expect(document.getElementById(regionId)!.hidden).toBe(false)
  })

  it('renders ToolResult when block.result is present', () => {
    const withResult: ToolBlock = {
      ...bashBlock,
      result: { ok: true, payload: { content: 'test passed' } },
    }
    render(<ToolCallCard block={withResult} />)
    expect(screen.getByText(/test passed/)).toBeInTheDocument()
  })
})
