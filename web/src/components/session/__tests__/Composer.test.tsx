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

  it('shows Stop and Queue buttons in working state', () => {
    render(<Composer {...defaults} status={{ state: 'working', count: 0 }} />)
    expect(screen.getByRole('button', { name: /stop current turn/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /queue message/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /send message/i })).not.toBeInTheDocument()
  })

  it('clicking Stop calls onInterrupt', async () => {
    const user = userEvent.setup()
    const onInterrupt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'working', count: 0 }} onInterrupt={onInterrupt} />)
    await user.click(screen.getByRole('button', { name: /stop current turn/i }))
    expect(onInterrupt).toHaveBeenCalledOnce()
  })

  it('clicking Queue while working calls onPrompt and clears input', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'working', count: 0 }} onPrompt={onPrompt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'upcoming task')
    await user.click(screen.getByRole('button', { name: /queue message/i }))
    expect(onPrompt).toHaveBeenCalledWith('upcoming task', [])
    expect(textarea).toHaveValue('')
  })

  it('while working, Stop still calls onInterrupt and Queue calls onPrompt independently', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    const onInterrupt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'working', count: 0 }} onPrompt={onPrompt} onInterrupt={onInterrupt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'queued message')
    // Queue it
    await user.click(screen.getByRole('button', { name: /queue message/i }))
    expect(onPrompt).toHaveBeenCalledWith('queued message', [])
    // Stop is still available
    await user.click(screen.getByRole('button', { name: /stop current turn/i }))
    expect(onInterrupt).toHaveBeenCalledOnce()
  })

  it('Enter submits as Queue while working and clears textarea', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'working', count: 0 }} onPrompt={onPrompt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'next step')
    await user.keyboard('{Enter}')
    expect(onPrompt).toHaveBeenCalledWith('next step', [])
    expect(textarea).toHaveValue('')
  })

  it('Enter submits and clears the textarea', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} onPrompt={onPrompt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'Hello world')
    await user.keyboard('{Enter}')
    expect(onPrompt).toHaveBeenCalledWith('Hello world', [])
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

  it('when chatArmed, shows a Reply button (not Queue) even while working', () => {
    render(<Composer {...defaults} status={{ state: 'working', count: 0 }} chatArmed />)
    expect(screen.getByRole('button', { name: /send reply/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /queue message/i })).not.toBeInTheDocument()
  })

  it('clicking Reply when chatArmed calls onPrompt and clears input', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'working', count: 0 }} chatArmed onPrompt={onPrompt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'lets chat')
    await user.click(screen.getByRole('button', { name: /send reply/i }))
    expect(onPrompt).toHaveBeenCalledWith('lets chat', [])
    expect(textarea).toHaveValue('')
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

  // ── Queued strip ────────────────────────────────────────────────────────────

  it('renders cancel buttons for each queued item', () => {
    render(
      <Composer
        {...defaults}
        queued={['first queued prompt', 'second queued prompt']}
        onCancelQueued={vi.fn()}
      />
    )
    const cancelButtons = screen.getAllByRole('button', { name: /cancel queued prompt/i })
    expect(cancelButtons).toHaveLength(2)
  })

  it('clicking cancel button calls onCancelQueued with correct index', async () => {
    const user = userEvent.setup()
    const onCancelQueued = vi.fn()
    render(
      <Composer
        {...defaults}
        queued={['first prompt', 'second prompt']}
        onCancelQueued={onCancelQueued}
      />
    )
    const cancelButtons = screen.getAllByRole('button', { name: /cancel queued prompt/i })
    // Click the second cancel button (index 1)
    await user.click(cancelButtons[1])
    expect(onCancelQueued).toHaveBeenCalledWith(1)
  })

  it('does not render queued strip when queued is empty', () => {
    render(<Composer {...defaults} queued={[]} onCancelQueued={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /cancel queued prompt/i })).not.toBeInTheDocument()
  })

  it('does not render queued strip when queued prop is not provided', () => {
    render(<Composer {...defaults} />)
    expect(screen.queryByRole('button', { name: /cancel queued prompt/i })).not.toBeInTheDocument()
  })
})
