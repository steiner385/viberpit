import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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

  it('existing suggestion: play sends it immediately', () => {
    const onPrompt = vi.fn()
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: ['Run the tests', 'Commit it'], onPrompt })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onPrompt).toHaveBeenCalledWith('Run the tests', [])
  })

  it('no suggestion: play prefills "continue" (focused), does not send', () => {
    const onPrompt = vi.fn()
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: [], onPrompt })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(textarea.value).toBe('continue')
    expect(onPrompt).not.toHaveBeenCalled()
    // prefill focuses the box so the user can immediately edit / press Enter
    expect(document.activeElement).toBe(textarea)
  })

  it('dropdown surfaces leftover dynamic suggestions and picking sends that item', () => {
    const onPrompt = vi.fn()
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: ['First', 'Second'], onPrompt })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Second' }))
    expect(onPrompt).toHaveBeenCalledWith('Second', [])
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
    expect(screen.getByRole('menuitem', { name: 'Second' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'First' })).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'continue' })).toBeInTheDocument()
    expect(screen.getByRole('separator')).toBeInTheDocument()
  })
})
