import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { RadioGroup } from './RadioGroup'

expect.extend(toHaveNoViolations)

const OPTS = [
  { value: 'system', label: 'Use system setting' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark', disabled: true, disabledReason: 'Pinned by the kiosk URL' },
] as const

describe('RadioGroup', () => {
  it('is a fieldset named by its legend; picks by click; disabled options say why', async () => {
    const onChange = vi.fn()
    const { container } = render(
      <RadioGroup name="theme" legend="Theme" value="system" onChange={onChange} options={[...OPTS]} />,
    )
    expect(screen.getByRole('group', { name: 'Theme' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('radio', { name: /Light/ }))
    expect(onChange).toHaveBeenCalledWith('light')
    const dark = screen.getByRole('radio', { name: /Dark/ })
    expect(dark).toBeDisabled()
    expect(screen.getByText('Pinned by the kiosk URL')).toBeInTheDocument()
    expect(await axe(container)).toHaveNoViolations()
  })

  it('hideLegend keeps the legend for AT but not on screen', () => {
    render(<RadioGroup name="t" legend="Theme" hideLegend value="system" onChange={() => {}} options={[...OPTS]} />)
    expect(screen.getByRole('group', { name: 'Theme' })).toBeInTheDocument()
    expect(screen.getByText('Theme')).toHaveClass('sr-only')
  })
})
