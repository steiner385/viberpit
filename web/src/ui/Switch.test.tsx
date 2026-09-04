import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Switch } from './Switch'

expect.extend(toHaveNoViolations)

describe('Switch', () => {
  it('is a switch whose accessible name does not change with state', async () => {
    const onChange = vi.fn()
    const { rerender } = render(<Switch checked={false} onChange={onChange} aria-label="Pipeline notifications" />)
    const sw = screen.getByRole('switch', { name: 'Pipeline notifications' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    await userEvent.click(sw)
    expect(onChange).toHaveBeenCalledWith(true)
    rerender(<Switch checked onChange={onChange} aria-label="Pipeline notifications" />)
    expect(screen.getByRole('switch', { name: 'Pipeline notifications' })).toHaveAttribute('aria-checked', 'true')
  })

  it('toggles on Space and Enter, not on other keys; disabled does nothing', async () => {
    const onChange = vi.fn()
    render(<Switch checked={false} onChange={onChange} aria-label="X" />)
    const sw = screen.getByRole('switch')
    sw.focus()
    await userEvent.keyboard(' ')
    await userEvent.keyboard('{Enter}')
    await userEvent.keyboard('a')
    expect(onChange).toHaveBeenCalledTimes(2)
    onChange.mockClear()
    render(<Switch checked={false} onChange={onChange} aria-label="Y" disabled />)
    await userEvent.click(screen.getByRole('switch', { name: 'Y' }))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('has no axe violations', async () => {
    const { container } = render(<Switch checked onChange={() => {}} aria-label="Enabled" />)
    expect(await axe(container)).toHaveNoViolations()
  })
})
