import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ToolCallCard } from './ToolCallCard'
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
    // Collapsed by default: the region uses `hidden={!subOpen}` (parity with the
    // sibling args/result panels), so the nested text is in the DOM but not visible.
    expect(screen.getByText('sub-agent says hello')).not.toBeVisible()
    // Expand the sub-agent region.
    await userEvent.click(screen.getByRole('button', { name: /sub-agent activity/i }))
    expect(screen.getByText('sub-agent says hello')).toBeVisible()
  })

  it('a nested turn\'s thinking toggle re-applies the mode default when the epoch bumps', async () => {
    const user = userEvent.setup()
    const withThinking: SubThread = {
      openBlockId: null,
      turns: [{ id: 'sa1', role: 'assistant', blocks: [{ kind: 'thinking', id: 'sa1:th', text: 'pondering' }] }],
    }
    const { rerender } = render(
      <ToolCallCard block={taskBlock} subThread={withThinking} thinkingCollapsed={false} resetKey={1} />,
    )
    await user.click(screen.getByRole('button', { name: /sub-agent activity/i }))
    const thinkingToggle = () => screen.getByRole('button', { name: /^thinking$/i, hidden: true })
    expect(thinkingToggle()).toHaveAttribute('aria-expanded', 'true')
    await user.click(thinkingToggle())
    expect(thinkingToggle()).toHaveAttribute('aria-expanded', 'false')
    // Same mode default, new epoch (Thinking → Verbose): the manual collapse is overridden.
    rerender(<ToolCallCard block={taskBlock} subThread={withThinking} thinkingCollapsed={false} resetKey={2} />)
    expect(thinkingToggle()).toHaveAttribute('aria-expanded', 'true')
  })

  it('does not render a sub-agent region for a non-Task tool', () => {
    const bash: Extract<Block, { kind: 'tool' }> = {
      kind: 'tool', id: 'b1', toolId: 'b1', name: 'Bash', toolKind: 'bash', input: { command: 'ls' },
    }
    render(<ToolCallCard block={bash} />)
    expect(screen.queryByRole('button', { name: /sub-agent activity/i })).not.toBeInTheDocument()
  })
})
