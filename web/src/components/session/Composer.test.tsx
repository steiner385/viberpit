import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Composer } from './Composer'

function setup(suggestions?: string[]) {
  const onPrompt = vi.fn()
  render(
    <Composer
      status={{ state: 'idle', count: 0 }}
      mode="default"
      onPrompt={onPrompt}
      onInterrupt={() => {}}
      onSetMode={() => {}}
      suggestions={suggestions}
    />,
  )
  return { onPrompt }
}

describe('Composer suggestions', () => {
  it('renders a chip per suggestion', () => {
    setup(['Run the tests', 'Commit it'])
    expect(screen.getByRole('button', { name: 'Run the tests' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Commit it' })).toBeInTheDocument()
  })

  it('pre-fills the textarea on chip click without sending', () => {
    const { onPrompt } = setup(['Run the tests'])
    fireEvent.click(screen.getByRole('button', { name: 'Run the tests' }))
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(textarea.value).toBe('Run the tests')
    expect(onPrompt).not.toHaveBeenCalled()
  })

  it('renders no chips when suggestions is empty', () => {
    setup([])
    expect(screen.queryByTestId('suggestion-chips')).toBeNull()
  })
})
