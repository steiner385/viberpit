import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Composer } from './Composer'

function defaultProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}): Parameters<typeof Composer>[0] {
  return {
    status: { state: 'idle', count: 0 },
    mode: 'default',
    onPrompt: vi.fn(),
    onInterrupt: vi.fn(),
    onSetMode: vi.fn(),
    onRequestSuggestions: vi.fn(),
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
      mode="default"
      onPrompt={onPrompt}
      onInterrupt={() => {}}
      onSetMode={() => {}}
      onRequestSuggestions={vi.fn()}
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

describe('Composer play button', () => {
  it('shows the play button only when idle and the box is empty', () => {
    const { rerender } = renderComposer({ status: { state: 'done', count: 0 }, suggestions: ['Run the tests'] })
    expect(screen.getByRole('button', { name: /resume/i })).toBeInTheDocument()
    rerender(<Composer {...defaultProps({ status: { state: 'in_progress', count: 0 }, suggestions: ['x'] })} />)
    expect(screen.queryByRole('button', { name: /resume/i })).not.toBeInTheDocument()
  })

  it('existing suggestion: play sends it immediately', () => {
    const onPrompt = vi.fn()
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: ['Run the tests', 'Commit it'], onPrompt })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onPrompt).toHaveBeenCalledWith('Run the tests', [])
  })

  it('no suggestion: play requests generation and does not send', () => {
    const onPrompt = vi.fn()
    const onRequestSuggestions = vi.fn()
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: [], onPrompt, onRequestSuggestions })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onRequestSuggestions).toHaveBeenCalledTimes(1)
    expect(onPrompt).not.toHaveBeenCalled()
  })

  it('generated suggestion prefills the box (confirm, not auto-send)', () => {
    const onPrompt = vi.fn()
    const { rerender } = renderComposer({ status: { state: 'done', count: 0 }, suggestions: [], onRequestSuggestions: vi.fn(), onPrompt })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    rerender(<Composer {...defaultProps({ status: { state: 'done', count: 0 }, suggestions: ['Finish the task'], onPrompt })} />)
    expect(screen.getByLabelText('Message')).toHaveValue('Finish the task')
    expect(onPrompt).not.toHaveBeenCalled()
  })

  it('caret menu appears with >=2 suggestions and picking sends that item', () => {
    const onPrompt = vi.fn()
    renderComposer({ status: { state: 'done', count: 0 }, suggestions: ['First', 'Second'], onPrompt })
    fireEvent.click(screen.getByRole('button', { name: /more suggestions/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Second' }))
    expect(onPrompt).toHaveBeenCalledWith('Second', [])
  })
})
