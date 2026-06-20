import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Composer } from '../Composer'

describe('Composer', () => {
  const defaults = {
    status: { state: 'idle', count: 0 },
    mode: 'interactive',
    onPrompt: vi.fn(),
    onInterrupt: vi.fn(),
    onSetMode: vi.fn(),
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows Send button in idle state', () => {
    render(<Composer {...defaults} />)
    expect(screen.getByRole('button', { name: /send message/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /stop/i })).not.toBeInTheDocument()
  })

  it('shows Stop button in working state', () => {
    render(<Composer {...defaults} status={{ state: 'working', count: 0 }} />)
    expect(screen.getByRole('button', { name: /stop current turn/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /send message/i })).not.toBeInTheDocument()
  })

  it('clicking Stop calls onInterrupt', async () => {
    const user = userEvent.setup()
    const onInterrupt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'working', count: 0 }} onInterrupt={onInterrupt} />)
    await user.click(screen.getByRole('button', { name: /stop current turn/i }))
    expect(onInterrupt).toHaveBeenCalledOnce()
  })

  it('Enter submits and clears the textarea', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} onPrompt={onPrompt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'Hello world')
    await user.keyboard('{Enter}')
    expect(onPrompt).toHaveBeenCalledWith('Hello world')
    expect(textarea).toHaveValue('')
  })

  it('Shift+Enter does NOT submit', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} onPrompt={onPrompt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'Hello')
    await user.keyboard('{Shift>}{Enter}{/Shift}')
    expect(onPrompt).not.toHaveBeenCalled()
    // textarea still has content
    expect(textarea).toHaveValue('Hello\n')
  })

  it('plan mode toggle shows aria-checked=false when not in plan mode', () => {
    render(<Composer {...defaults} mode="interactive" />)
    const toggle = screen.getByRole('switch', { name: /plan mode/i })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
  })

  it('plan mode toggle shows aria-checked=true when in plan mode', () => {
    render(<Composer {...defaults} mode="plan" />)
    const toggle = screen.getByRole('switch', { name: /plan mode/i })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('clicking plan toggle calls onSetMode with "plan" when not in plan mode', async () => {
    const user = userEvent.setup()
    const onSetMode = vi.fn()
    render(<Composer {...defaults} mode="interactive" onSetMode={onSetMode} />)
    await user.click(screen.getByRole('switch', { name: /plan mode/i }))
    expect(onSetMode).toHaveBeenCalledWith('plan')
  })

  it('clicking plan toggle calls onSetMode with "default" when in plan mode', async () => {
    const user = userEvent.setup()
    const onSetMode = vi.fn()
    render(<Composer {...defaults} mode="plan" onSetMode={onSetMode} />)
    await user.click(screen.getByRole('switch', { name: /plan mode/i }))
    expect(onSetMode).toHaveBeenCalledWith('default')
  })

  it('does not submit empty/whitespace-only text', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} onPrompt={onPrompt} />)
    await user.keyboard('{Enter}')
    expect(onPrompt).not.toHaveBeenCalled()
  })
})
