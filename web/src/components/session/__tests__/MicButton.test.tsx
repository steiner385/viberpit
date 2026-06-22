import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MicButton } from '../MicButton'

describe('MicButton', () => {
  it('renders nothing when speech is unsupported', () => {
    const { container } = render(
      <MicButton supported={false} listening={false} error={null} onToggle={() => {}} />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('labels itself for starting when idle and fires onToggle', async () => {
    const onToggle = vi.fn()
    render(<MicButton supported listening={false} error={null} onToggle={onToggle} />)
    const btn = screen.getByRole('button', { name: 'Start dictation' })
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    await userEvent.click(btn)
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('labels itself for stopping while listening', () => {
    render(<MicButton supported listening error={null} onToggle={() => {}} />)
    const btn = screen.getByRole('button', { name: 'Stop dictation' })
    expect(btn).toHaveAttribute('aria-pressed', 'true')
  })

  it('exposes the error via title when present', () => {
    render(<MicButton supported listening={false} error="not-allowed" onToggle={() => {}} />)
    expect(screen.getByRole('button')).toHaveAttribute('title', expect.stringContaining('not-allowed'))
  })
})
