import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Button } from './Button'

expect.extend(toHaveNoViolations)

describe('Button', () => {
  it('renders the default variant on the accent token', () => {
    render(<Button>Save</Button>)
    const btn = screen.getByRole('button', { name: 'Save' })
    expect(btn.className).toContain('bg-accent')
    expect(btn.className).toContain('text-accent-ink')
    expect(btn.className).not.toMatch(/bg-primary|dark:/)
  })
  it('renders outline on surface/edge tokens', () => {
    render(<Button variant="outline">Cancel</Button>)
    expect(screen.getByRole('button').className).toContain('border-edge')
  })
  it('has no axe violations', async () => {
    const { container } = render(<Button>Go</Button>)
    expect(await axe(container)).toHaveNoViolations()
  })
})
