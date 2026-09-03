import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Loader2 } from 'lucide-react'
import { Card } from './Card'
import { Badge } from './Badge'
import { StatusPill } from './StatusPill'
import { UsageRing } from './UsageRing'
import { Kbd } from './Kbd'

expect.extend(toHaveNoViolations)

describe('pills & surfaces', () => {
  it('render on tokens with accessible names', async () => {
    const { container } = render(
      <Card tone="warning" interactive>
        <StatusPill label="Working…" Icon={Loader2} tone="success" />
        <Badge tone="neutral" count={12} />
        <UsageRing value={0.42} label="Context 42%" />
        <Kbd keys="Cmd+K" />
      </Card>,
    )
    expect(screen.getByLabelText('Status: Working…').className).toContain('text-success')
    expect(screen.getByText('12').className).toContain('bg-neutral-wash')
    expect(screen.getByRole('img', { name: 'Context 42%' })).toBeInTheDocument()
    expect(container.querySelector('[data-tone="warning"]')).not.toBeNull()
    expect(container.innerHTML).not.toMatch(/dark:|gray-|amber-/)
    expect(await axe(container)).toHaveNoViolations()
  })

  it('renders no status rail when no tone is given', () => {
    const { container } = render(<Card>plain</Card>)
    expect(container.querySelector('[data-tone]')).toBeNull()
    // The toned case above renders exactly one rail <span>; an untoned Card must render none.
    expect(container.querySelectorAll('span[aria-hidden="true"]').length).toBe(0)
  })
})
