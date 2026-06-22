import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ToolCallCard } from '../session/ToolCallCard'
import type { Block, SubThread } from '@/session/reducer'

const taskBlock: Extract<Block, { kind: 'tool' }> = {
  kind: 'tool', id: 'task1', toolId: 'task1', name: 'Task', toolKind: 'unknown',
  input: { subagent_type: 'Explore', description: 'find the bug' },
}

const subThread: SubThread = {
  openBlockId: null,
  turns: [{ id: 'sa1', role: 'assistant', blocks: [
    { kind: 'text', id: 'sa1:t0', text: 'sub-agent says hello' },
  ] }],
}

describe('ToolCallCard sub-agent thread', () => {
  it('renders the nested sub-agent transcript when expanded', async () => {
    render(<ToolCallCard block={taskBlock} subThread={subThread} />)
    // Collapsed by default: nested text not visible yet.
    expect(screen.queryByText('sub-agent says hello')).not.toBeInTheDocument()
    // Expand the sub-agent region.
    await userEvent.click(screen.getByRole('button', { name: /sub-agent activity/i }))
    expect(screen.getByText('sub-agent says hello')).toBeInTheDocument()
  })

  it('does not render a sub-agent region for a non-Task tool', () => {
    const bash: Extract<Block, { kind: 'tool' }> = {
      kind: 'tool', id: 'b1', toolId: 'b1', name: 'Bash', toolKind: 'bash', input: { command: 'ls' },
    }
    render(<ToolCallCard block={bash} />)
    expect(screen.queryByRole('button', { name: /sub-agent activity/i })).not.toBeInTheDocument()
  })
})
