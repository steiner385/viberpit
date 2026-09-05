import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Checkbox } from './Checkbox'

expect.extend(toHaveNoViolations)

describe('Checkbox', () => {
  it('is a labelled native checkbox that reports the next value', async () => {
    const onChange = vi.fn()
    const { container } = render(<Checkbox id="c" label="Collapse thinking by default" checked={false} onChange={onChange} />)
    const box = screen.getByRole('checkbox', { name: 'Collapse thinking by default' })
    await userEvent.click(box)
    expect(onChange).toHaveBeenCalledWith(true)
    expect(await axe(container)).toHaveNoViolations()
  })
})
