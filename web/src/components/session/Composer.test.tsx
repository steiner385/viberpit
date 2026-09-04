import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Composer } from './Composer'
import { QUICK_REPLIES } from '@/lib/quickReplies'

function defaultProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}): Parameters<typeof Composer>[0] {
  return {
    status: { state: 'idle', count: 0 },
    onPrompt: vi.fn(),
    onInterrupt: vi.fn(),
    suggestions: [],
    ...overrides,
  }
}

function renderComposer(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return render(<Composer {...defaultProps(overrides)} />)
}

function setupStatus(suggestions: string[] | undefined, state = 'idle') {
  const onPrompt = vi.fn()
  render(
    <Composer
      status={{ state, count: 0 }}
      onPrompt={onPrompt}
      onInterrupt={() => {}}
      suggestions={suggestions}
    />,
  )
  return { onPrompt }
}

describe('Composer ghost-text suggestion', () => {
  it('shows the top suggestion as ghost text when idle and empty', () => {
    setupStatus(['Run the tests', 'Commit it'])
    const ghost = screen.getByTestId('ghost-suggestion')
    expect(ghost).toHaveTextContent('Run the tests')
    expect(ghost).toHaveTextContent('Tab')
  })

  it('hides ghost text while working', () => {
    setupStatus(['Run the tests'], 'in_progress')
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })

  it('hides ghost text when there are no suggestions', () => {
    setupStatus([])
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })

  it('Tab fills the box without sending and hides the ghost', () => {
    const { onPrompt } = setupStatus(['Run the tests'])
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.keyDown(textarea, { key: 'Tab' })
    expect(textarea.value).toBe('Run the tests')
    expect(onPrompt).not.toHaveBeenCalled()
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })

  it('ArrowRight fills the box without sending', () => {
    const { onPrompt } = setupStatus(['Run the tests'])
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.keyDown(textarea, { key: 'ArrowRight' })
    expect(textarea.value).toBe('Run the tests')
    expect(onPrompt).not.toHaveBeenCalled()
  })

  it('tapping the ghost fills the box without sending (touch path)', () => {
    const { onPrompt } = setupStatus(['Run the tests'])
    fireEvent.click(screen.getByTestId('ghost-suggestion'))
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(textarea.value).toBe('Run the tests')
    expect(onPrompt).not.toHaveBeenCalled()
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })

  it('Shift+Tab does not accept the suggestion', () => {
    setupStatus(['Run the tests'])
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.keyDown(textarea, { key: 'Tab', shiftKey: true })
    expect(textarea.value).toBe('')
  })

  it('hides ghost text once the user types', () => {
    setupStatus(['Run the tests'])
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: 'h' } })
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })
})

describe('Composer sparkle button', () => {
  it('shows the sparkle button only when idle and the box is empty', () => {
    const { rerender } = renderComposer({ status: { state: 'done', count: 0 }, suggestions: ['Run the tests'] })
    expect(screen.getByRole('button', { name: /resume/i })).toBeInTheDocument()
    rerender(<Composer {...defaultProps({ status: { state: 'in_progress', count: 0 }, suggestions: ['x'] })} />)
    expect(screen.queryByRole('button', { name: /resume/i })).not.toBeInTheDocument()
  })

  it('renders the AI sparkle glyph (not the old play triangle)', () => {
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: [] })
    const resume = screen.getByRole('button', { name: /resume/i })
    expect(resume.querySelector('.lucide-sparkles')).toBeInTheDocument()
    expect(resume.querySelector('.lucide-play')).not.toBeInTheDocument()
  })

  it('sparkle ALWAYS asks the LLM and sends the top freshly-generated suggestion', async () => {
    const onPrompt = vi.fn()
    const onRequestSuggestions = vi.fn().mockResolvedValue(['Generated next step', 'Another'])
    // A stale cached suggestion must be ignored — the sparkle generates fresh.
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: ['stale cached'], onPrompt, onRequestSuggestions })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    await waitFor(() => expect(onRequestSuggestions).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(onPrompt).toHaveBeenCalledWith('Generated next step', []))
  })

  it('empty generation: sparkle sends nothing and never falls back to "continue"', async () => {
    const onPrompt = vi.fn()
    const onRequestSuggestions = vi.fn().mockResolvedValue([])
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: [], onPrompt, onRequestSuggestions })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    await waitFor(() => expect(onRequestSuggestions).toHaveBeenCalled())
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(onPrompt).not.toHaveBeenCalled()
    expect(textarea.value).toBe('') // no hard-coded "continue" prefill
  })

  it('dropdown surfaces all cached dynamic suggestions and picking sends that item', () => {
    const onPrompt = vi.fn()
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: ['First', 'Second'], onPrompt })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'First' }))
    expect(onPrompt).toHaveBeenCalledWith('First', [])
  })

  it('quick-replies dropdown is available with no backend suggestions', () => {
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: [] })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    for (const reply of QUICK_REPLIES) {
      expect(screen.getByRole('menuitem', { name: reply })).toBeInTheDocument()
    }
  })

  it('clicking a canned quick reply sends its literal text immediately', () => {
    const onPrompt = vi.fn()
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: [], onPrompt })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'proceed' }))
    expect(onPrompt).toHaveBeenCalledWith('proceed', [])
  })

  it('lists dynamic suggestions above the canned list, separated by a divider', () => {
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: ['First', 'Second'] })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    expect(screen.getByRole('menuitem', { name: 'First' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Second' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'continue' })).toBeInTheDocument()
    expect(screen.getByRole('separator')).toBeInTheDocument()
  })
})

describe('Composer activity ping', () => {
  it('calls onActivity while the user types', () => {
    const onActivity = vi.fn()
    renderComposer({ onActivity })
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: 'hel' } })
    expect(onActivity).toHaveBeenCalled()
  })
})

describe('Composer interrupt-and-send ("Send now")', () => {
  const busy = { state: 'in_progress', count: 0 }

  function type(value: string) {
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value } })
    return textarea
  }

  it('offers Send now only while a turn is busy', () => {
    const { rerender } = renderComposer({ status: busy, onInterruptAndPrompt: vi.fn() })
    expect(screen.getByRole('button', { name: /send now/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /queue message/i })).toBeInTheDocument()
    rerender(<Composer {...defaultProps({ status: { state: 'idle', count: 0 }, onInterruptAndPrompt: vi.fn() })} />)
    expect(screen.queryByRole('button', { name: /send now/i })).not.toBeInTheDocument()
  })

  it('Send now interrupts-and-posts the draft instead of queueing it', () => {
    const onPrompt = vi.fn()
    const onInterrupt = vi.fn()
    const onInterruptAndPrompt = vi.fn().mockReturnValue(true)
    renderComposer({ status: busy, onPrompt, onInterrupt, onInterruptAndPrompt })
    const textarea = type('do this instead')
    fireEvent.click(screen.getByRole('button', { name: /send now/i }))
    expect(onInterruptAndPrompt).toHaveBeenCalledWith('do this instead', [])
    expect(onPrompt).not.toHaveBeenCalled()
    expect(onInterrupt).not.toHaveBeenCalled()
    expect(textarea.value).toBe('')
  })

  it('Ctrl/Cmd+Enter while busy is the keyboard path to Send now; plain Enter still queues', () => {
    const onPrompt = vi.fn()
    const onInterruptAndPrompt = vi.fn().mockReturnValue(true)
    renderComposer({ status: busy, onPrompt, onInterruptAndPrompt })
    let textarea = type('now please')
    fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true })
    expect(onInterruptAndPrompt).toHaveBeenCalledWith('now please', [])
    expect(onPrompt).not.toHaveBeenCalled()

    textarea = type('later is fine')
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect(onPrompt).toHaveBeenCalledWith('later is fine', [])
    expect(onInterruptAndPrompt).toHaveBeenCalledTimes(1)
  })

  it('keeps the draft when the interrupt-and-post could not be sent', () => {
    const onInterruptAndPrompt = vi.fn().mockReturnValue(false)
    renderComposer({ status: busy, onInterruptAndPrompt })
    const textarea = type('keep me')
    fireEvent.click(screen.getByRole('button', { name: /send now/i }))
    expect(onInterruptAndPrompt).toHaveBeenCalledWith('keep me', [])
    expect(textarea.value).toBe('keep me')
  })

  it('Send now with an empty draft does nothing', () => {
    const onInterruptAndPrompt = vi.fn()
    renderComposer({ status: busy, onInterruptAndPrompt })
    fireEvent.click(screen.getByRole('button', { name: /send now/i }))
    expect(onInterruptAndPrompt).not.toHaveBeenCalled()
  })
})
