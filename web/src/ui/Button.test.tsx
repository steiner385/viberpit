import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Button } from './Button'

expect.extend(toHaveNoViolations)

describe('Button', () => {
  it('renders primary on the accent token by default', () => {
    render(<Button>Save</Button>)
    const btn = screen.getByRole('button', { name: 'Save' })
    expect(btn.className).toContain('bg-accent')
    expect(btn.className).not.toMatch(/bg-primary|dark:|gray-/)
  })
  it('secondary uses surface/edge; danger uses the danger token', () => {
    render(<><Button variant="secondary">A</Button><Button variant="danger">B</Button></>)
    expect(screen.getByText('A').className).toContain('border-edge')
    expect(screen.getByText('B').className).toContain('bg-danger')
  })
  it('loading disables and shows a spinner', () => {
    render(<Button loading>Save</Button>)
    const btn = screen.getByRole('button')
    expect(btn).toBeDisabled()
    expect(btn.querySelector('[data-spinner]')).not.toBeNull()
  })
  it('danger uses the theme-invariant strong fill (white text must stay AA in dark mode)', () => {
    render(<Button variant="danger">Delete</Button>)
    const btn = screen.getByRole('button', { name: 'Delete' })
    expect(btn.className).toContain('bg-danger-strong')
    expect(btn.className).not.toMatch(/bg-danger(?!-strong)/)
  })
  it('has no axe violations', async () => {
    const { container } = render(<Button>Go</Button>)
    expect(await axe(container)).toHaveNoViolations()
  })
})
