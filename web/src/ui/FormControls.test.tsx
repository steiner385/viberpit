import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Input } from './Input'
import { Textarea } from './Textarea'
import { Select } from './Select'
import { Field } from './Field'

// Scope note: axe in jsdom covers roles, names and ARIA wiring only. No
// stylesheet is applied here, so axe's `color-contrast` rule always returns
// `incomplete`, and `toHaveNoViolations()` inspects `violations` alone — these
// assertions therefore cannot fail on colour. Contrast is covered instead by
// src/__tests__/contrast.test.ts, which does the WCAG maths on index.css.
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
    expect(input.getAttribute('aria-describedby')).toBe('n-help n-error')
    expect(screen.getByText('Required')).toHaveAttribute('role', 'alert')
    expect(await axe(container)).toHaveNoViolations()
  })
  it('Field with help only: aria-describedby is exactly the help id', () => {
    render(
      <Field label="Name" htmlFor="n" help="Shown in the list">
        <Input id="n" />
      </Field>,
    )
    expect(screen.getByLabelText('Name').getAttribute('aria-describedby')).toBe('n-help')
  })
  it('Field with error only: aria-describedby is exactly the error id', () => {
    render(
      <Field label="Name" htmlFor="n" error="Required">
        <Input id="n" />
      </Field>,
    )
    const input = screen.getByLabelText('Name')
    expect(input.getAttribute('aria-describedby')).toBe('n-error')
    expect(screen.getByText('Required')).toHaveAttribute('role', 'alert')
  })
  it('Field with neither help nor error: control has no aria-describedby at all', () => {
    render(
      <Field label="Name" htmlFor="n">
        <Input id="n" />
      </Field>,
    )
    expect(screen.getByLabelText('Name')).not.toHaveAttribute('aria-describedby')
  })
  it('Field preserves a child\'s own aria-describedby and appends its own ids after it', () => {
    render(
      <Field label="Name" htmlFor="n" help="Shown in the list" error="Required">
        <Input id="n" aria-describedby="extra-hint" />
      </Field>,
    )
    expect(screen.getByLabelText('Name').getAttribute('aria-describedby')).toBe('extra-hint n-help n-error')
  })
  it('Field forwards required to the child when set', () => {
    render(
      <Field label="Name" htmlFor="n" required>
        <Input id="n" />
      </Field>,
    )
    expect(screen.getByLabelText('Name', { exact: false })).toBeRequired()
  })
  it("Field does not clobber a child's own required when Field omits it", () => {
    render(
      <Field label="Name" htmlFor="n">
        <Input id="n" required />
      </Field>,
    )
    expect(screen.getByLabelText('Name')).toBeRequired()
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
