import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Composer } from './Composer'

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
