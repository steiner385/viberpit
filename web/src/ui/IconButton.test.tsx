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

  it('requires aria-label at the type level', () => {
    // NOTE: inert today — tsconfig.app.json excludes *.test.tsx, so nothing type-checks
    // this file (see the typecheck-gate follow-up). Kept because editors surface it now,
    // and it becomes a real gate failure once test files are checked.
    // @ts-expect-error — an icon-only button has no visible text, so aria-label is required.
    const missingLabel = <IconButton><X size={16} /></IconButton>
    expect(missingLabel).toBeTruthy()
  })
})
