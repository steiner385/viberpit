import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { X } from 'lucide-react'
import { IconButton } from './IconButton'

expect.extend(toHaveNoViolations)

describe('IconButton', () => {
  it('requires an accessible name and meets the target size', async () => {
    const { container } = render(<IconButton aria-label="Close"><X /></IconButton>)
    const btn = screen.getByRole('button', { name: 'Close' })
    expect(btn.className).toContain('min-h-target')
    expect(await axe(container)).toHaveNoViolations()
  })
})
