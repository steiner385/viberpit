import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Input } from './Input'
import { Textarea } from './Textarea'
import { Select } from './Select'
import { Field } from './Field'

expect.extend(toHaveNoViolations)

describe('form controls', () => {
  it('Field wires label, help and error to the control', async () => {
    const { container } = render(
      <Field label="Name" htmlFor="n" help="Shown in the list" error="Required">
        <Input id="n" invalid />
      </Field>,
    )
    const input = screen.getByLabelText('Name')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input.getAttribute('aria-describedby')).toContain('n-help')
    expect(input.getAttribute('aria-describedby')).toContain('n-error')
    expect(screen.getByText('Required')).toHaveAttribute('role', 'alert')
    expect(await axe(container)).toHaveNoViolations()
  })
  it('Textarea autoGrow sets height from scrollHeight', () => {
    render(<Textarea aria-label="Prompt" autoGrow defaultValue="a" />)
    const ta = screen.getByLabelText('Prompt') as HTMLTextAreaElement
    Object.defineProperty(ta, 'scrollHeight', { value: 120, configurable: true })
    fireEvent.input(ta, { target: { value: 'a\nb\nc' } })
    expect(ta.style.height).toBe('120px')
  })
  it('Textarea autoGrow clamps to the default 192px cap (reuses lib/autoGrow)', () => {
    render(<Textarea aria-label="Long paste" autoGrow defaultValue="a" />)
    const ta = screen.getByLabelText('Long paste') as HTMLTextAreaElement
    Object.defineProperty(ta, 'scrollHeight', { value: 5000, configurable: true })
    fireEvent.input(ta, { target: { value: 'a very long pasted block'.repeat(200) } })
    expect(ta.style.height).toBe('192px')
  })
  it('Textarea autoGrow respects a custom maxHeightPx cap', () => {
    render(<Textarea aria-label="Capped" autoGrow maxHeightPx={80} defaultValue="a" />)
    const ta = screen.getByLabelText('Capped') as HTMLTextAreaElement
    Object.defineProperty(ta, 'scrollHeight', { value: 500, configurable: true })
    fireEvent.input(ta, { target: { value: 'x'.repeat(500) } })
    expect(ta.style.height).toBe('80px')
  })
  it('Select renders options with token classes', () => {
    render(<Select aria-label="Mode" defaultValue="a"><option value="a">A</option></Select>)
    expect(screen.getByLabelText('Mode').className).toContain('border-edge')
  })
})
