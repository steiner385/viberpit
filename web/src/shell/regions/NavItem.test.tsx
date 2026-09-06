import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Menu } from 'lucide-react'
import { NavItem } from './NavItem'

// Scope note: axe in jsdom covers roles, names and ARIA wiring only. No
// stylesheet is applied here, so axe's `color-contrast` rule always returns
// `incomplete`, and `toHaveNoViolations()` inspects `violations` alone — these
// assertions therefore cannot fail on colour. Contrast is covered instead by
// src/__tests__/contrast.test.ts, which does the WCAG maths on index.css.
expect.extend(toHaveNoViolations)

describe('NavItem — label-less count + attention', () => {
  // Task 14 deleted the `PhoneNav` case for this combination ("count + attention
  // folded into one indicator") because no real `NAV_DEFS` badge can produce it
  // any more: only `settings` still declares one, and it is boolean-only, so
  // "More" can carry attention but never a count in production. Deleting it FROM
  // THAT FILE was correct — feeding `settings: 2` through `NavBadges` there would
  // have been a lie about the badge convention. But the branch it exercised is
  // still live and generic (`NavItem`, not `PhoneNav`), so the coverage moves
  // here and drives it through real public props instead of a badge-map shape
  // that cannot occur: same shape as `Tabs.test.tsx`'s "composes the combined
  // count + attention accessible name".
  it('renders one badge carrying the count, in the attention tone', async () => {
    const { container } = render(
      <MemoryRouter>
        <NavItem icon={Menu} label="More" count={2} attention onClick={vi.fn()} />
      </MemoryRouter>,
    )
    const button = screen.getByRole('button', { name: 'More (2, needs attention)' })
    // One indicator, not two: label-less mode never draws a separate attention
    // dot alongside the count pill — see the `showLabel` branch in NavItem.tsx.
    expect(button.querySelectorAll('[class*="rounded-pill"]')).toHaveLength(1)
    const badge = button.querySelector('[class*="rounded-pill"]')
    expect(badge).toHaveTextContent('2')
    expect(badge?.className).toContain('text-warning')
    expect(await axe(container)).toHaveNoViolations()
  })
})
