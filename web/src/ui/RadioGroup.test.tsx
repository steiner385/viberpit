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

  it('horizontal orientation lays the options on one wrapping row and keeps the group semantics', async () => {
    const { container } = render(
      <RadioGroup name="theme" legend="Theme" orientation="horizontal" value="light" onChange={() => {}}
        options={[{ value: 'system', label: 'System' }, { value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }]} />,
    )
    const group = screen.getByRole('group', { name: 'Theme' })
    expect(screen.getAllByRole('radio')).toHaveLength(3)
    expect(screen.getByRole('radio', { name: 'Light' })).toBeChecked()
    // The options' wrapper is the row; the legend stays above it.
    const row = group.querySelector('legend + div')!
    expect(row.className).toMatch(/\bflex-wrap\b/)
    expect(row.className).not.toMatch(/\bflex-col\b/)
    expect(await axe(container)).toHaveNoViolations()
  })
})
