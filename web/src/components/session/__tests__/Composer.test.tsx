import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Composer } from '../Composer'

describe('Composer', () => {
  const defaults = {
    status: { state: 'idle', count: 0 },
    onPrompt: vi.fn(),
    onInterrupt: vi.fn(),
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('auto-focuses the message textarea on mount', () => {
    render(<Composer {...defaults} />)
    expect(screen.getByRole('textbox', { name: /message/i })).toHaveFocus()
  })

  it('re-focuses the textarea when autoFocusKey changes', () => {
    const { rerender } = render(<Composer {...defaults} autoFocusKey="a" />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    textarea.blur()
    expect(textarea).not.toHaveFocus()
    rerender(<Composer {...defaults} autoFocusKey="b" />)
    expect(textarea).toHaveFocus()
  })

  it('renders the textarea as a block element so it aligns with the button row', () => {
    // Contract: a default inline-block textarea sits on its wrapper's text baseline, leaving a
    // ~6px line-box descent below it. With `items-end` on the row that gap pushes the box above
    // the send/mic buttons. `block` removes it. jsdom has no layout, so we assert the class.
    render(<Composer {...defaults} />)
    expect(screen.getByRole('textbox', { name: /message/i })).toHaveClass('block')
  })

  it('pads its bottom by the safe-area inset so the input clears the mobile gesture bar', () => {
    // Regression guard (#327 header rework + h-dvh): on edge-to-edge mobile browsers
    // `100dvh` extends behind the home/gesture bar, so without a safe-area inset the
    // composer's last row is painted under the system bar and looks cut off. jsdom has
    // no layout, so we assert the inline style carries the env() inset.
    const { container } = render(<Composer {...defaults} />)
    const root = container.firstChild as HTMLElement
    expect(root.style.paddingBottom).toContain('env(safe-area-inset-bottom)')
  })

  it('shows Send button in idle state', () => {
    render(<Composer {...defaults} />)
    expect(screen.getByRole('button', { name: /send message/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /stop/i })).not.toBeInTheDocument()
  })

  it('shows Stop and Queue buttons in in_progress state', () => {
    render(<Composer {...defaults} status={{ state: 'in_progress', count: 0 }} />)
    expect(screen.getByRole('button', { name: /stop current turn/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /queue message/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /send message/i })).not.toBeInTheDocument()
  })

  it('clicking Stop calls onInterrupt', async () => {
    const user = userEvent.setup()
    const onInterrupt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'in_progress', count: 0 }} onInterrupt={onInterrupt} />)
    await user.click(screen.getByRole('button', { name: /stop current turn/i }))
    expect(onInterrupt).toHaveBeenCalledOnce()
  })

  it('clicking Queue while in_progress calls onPrompt and clears input', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'in_progress', count: 0 }} onPrompt={onPrompt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'upcoming task')
    await user.click(screen.getByRole('button', { name: /queue message/i }))
    expect(onPrompt).toHaveBeenCalledWith('upcoming task', [])
    expect(textarea).toHaveValue('')
  })

  it('while in_progress, Stop still calls onInterrupt and Queue calls onPrompt independently', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    const onInterrupt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'in_progress', count: 0 }} onPrompt={onPrompt} onInterrupt={onInterrupt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'queued message')
    // Queue it
    await user.click(screen.getByRole('button', { name: /queue message/i }))
    expect(onPrompt).toHaveBeenCalledWith('queued message', [])
    // Stop is still available
    await user.click(screen.getByRole('button', { name: /stop current turn/i }))
    expect(onInterrupt).toHaveBeenCalledOnce()
  })

  it('Enter submits as Queue while in_progress and clears textarea', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'in_progress', count: 0 }} onPrompt={onPrompt} />)
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

  it('when chatArmed, shows a Reply button (not Queue) even while in_progress', () => {
    render(<Composer {...defaults} status={{ state: 'in_progress', count: 0 }} chatArmed />)
    expect(screen.getByRole('button', { name: /send reply/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /queue message/i })).not.toBeInTheDocument()
  })

  it('clicking Reply when chatArmed calls onPrompt and clears input', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} status={{ state: 'in_progress', count: 0 }} chatArmed onPrompt={onPrompt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, 'lets chat')
    await user.click(screen.getByRole('button', { name: /send reply/i }))
    expect(onPrompt).toHaveBeenCalledWith('lets chat', [])
    expect(textarea).toHaveValue('')
  })

  it('does not submit empty/whitespace-only text', async () => {
    const user = userEvent.setup()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} onPrompt={onPrompt} />)
    await user.keyboard('{Enter}')
    expect(onPrompt).not.toHaveBeenCalled()
  })

  // ── Slash commands ──────────────────────────────────────────────────────────

  const goalCmd = { name: 'goal', description: 'Set the goal', argumentHint: '<text>', kind: 'passthrough' as const }
  const clearCmd = { name: 'clear', description: 'Clear conversation', argumentHint: null, kind: 'control' as const }

  it('runs a slash command with trailing text as args on Enter', async () => {
    const user = userEvent.setup()
    const onCommand = vi.fn()
    const onPrompt = vi.fn()
    render(<Composer {...defaults} commands={[goalCmd]} onCommand={onCommand} onPrompt={onPrompt} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, '/goal ship the web UI')
    await user.keyboard('{Enter}')
    expect(onCommand).toHaveBeenCalledWith('goal', 'ship the web UI')
    expect(onPrompt).not.toHaveBeenCalled()
    expect(textarea).toHaveValue('')
  })

  it('preserves multi-word args verbatim', async () => {
    const user = userEvent.setup()
    const onCommand = vi.fn()
    render(<Composer {...defaults} commands={[goalCmd]} onCommand={onCommand} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, '/goal   keep   inner spacing')
    await user.keyboard('{Enter}')
    expect(onCommand).toHaveBeenCalledWith('goal', 'keep   inner spacing')
  })

  it('runs a slash command with no args when none are typed', async () => {
    const user = userEvent.setup()
    const onCommand = vi.fn()
    render(<Composer {...defaults} commands={[clearCmd]} onCommand={onCommand} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, '/clear')
    await user.keyboard('{Enter}')
    expect(onCommand).toHaveBeenCalledWith('clear', '')
    expect(textarea).toHaveValue('')
  })

  it('keeps the palette open while still typing the command name', async () => {
    const user = userEvent.setup()
    render(<Composer {...defaults} commands={[goalCmd]} onCommand={vi.fn()} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, '/go')
    expect(screen.getByRole('listbox', { name: /slash commands/i })).toBeInTheDocument()
  })

  it('hides the palette once the user types a space after the command name', async () => {
    const user = userEvent.setup()
    render(<Composer {...defaults} commands={[goalCmd]} onCommand={vi.fn()} />)
    const textarea = screen.getByRole('textbox', { name: /message/i })
    await user.type(textarea, '/goal ')
    expect(screen.queryByRole('listbox', { name: /slash commands/i })).not.toBeInTheDocument()
  })

  // ── Queued strip ────────────────────────────────────────────────────────────

  it('renders cancel buttons for each queued item', () => {
    render(
      <Composer
        {...defaults}
        queued={[
          { text: 'first queued prompt', images: [] },
          { text: 'second queued prompt', images: [] },
        ]}
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
        queued={[
          { text: 'first prompt', images: [] },
          { text: 'second prompt', images: [] },
        ]}
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

  // ── History recall ───────────────────────────────────────────────────────────

  it('ArrowUp on empty input recalls the most recent prompt, then older', async () => {
    const user = userEvent.setup()
    render(<Composer {...defaults} history={['older', 'newer']} />)
    const box = screen.getByRole('textbox', { name: /message/i })
    box.focus()
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveValue('newer')
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveValue('older')
    await user.keyboard('{ArrowUp}') // clamped at oldest
    expect(box).toHaveValue('older')
  })

  it('ArrowDown walks back toward the live draft and restores it', async () => {
    const user = userEvent.setup()
    render(<Composer {...defaults} history={['older', 'newer']} />)
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.type(box, 'draft in progress')
    await user.keyboard('{ArrowUp}')          // stashes draft, shows newest
    expect(box).toHaveValue('newer')
    await user.keyboard('{ArrowDown}')         // back to draft
    expect(box).toHaveValue('draft in progress')
  })

  it('typing after a recall detaches history (next ArrowUp starts from newest)', async () => {
    const user = userEvent.setup()
    render(<Composer {...defaults} history={['older', 'newer']} />)
    const box = screen.getByRole('textbox', { name: /message/i })
    box.focus()
    await user.keyboard('{ArrowUp}{ArrowUp}')  // now showing 'older'
    await user.type(box, 'X')                  // edit detaches
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveValue('newer')
  })

  it('does not recall history while the slash-command palette is open', async () => {
    const user = userEvent.setup()
    const goalCmd = { name: 'goal', description: 'set goal', argumentHint: '' }
    render(<Composer {...defaults} history={['older', 'newer']} commands={[goalCmd]} onCommand={vi.fn()} />)
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.type(box, '/g')                 // palette is picking a command
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveValue('/g')              // palette consumed ↑, no history recall
  })

  it('does not recall history when slash-prefix matches NO command (unmatched slash)', async () => {
    const user = userEvent.setup()
    const goalCmd = { name: 'goal', description: 'set goal', argumentHint: '' }
    render(<Composer {...defaults} history={['older', 'newer']} commands={[goalCmd]} onCommand={vi.fn()} />)
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.type(box, '/zzz')               // pickingCommand=true, palette has zero matches
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveValue('/zzz')            // typed text must be preserved — no recall
  })

  it('ArrowUp does nothing when there is no history', async () => {
    const user = userEvent.setup()
    render(<Composer {...defaults} history={[]} />)
    const box = screen.getByRole('textbox', { name: /message/i })
    box.focus()
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveValue('')
  })
})
