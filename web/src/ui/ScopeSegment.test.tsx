// The one scope switch under every list column. The property under test is
// that it is a NAVIGATION holding no state: links to the scope's path,
// `aria-current` from the prop the route produced, and a name/count split a
// screen reader hears as "Inbox (9)", never "Inbox9".
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { MemoryRouter } from 'react-router-dom'
import { ScopeSegment } from './ScopeSegment'

expect.extend(toHaveNoViolations)

const items = [
  { to: '/x/map', label: 'Map', count: 6, current: true },
  { to: '/x/inbox', label: 'Inbox', count: 9, current: false, awayTone: 'warning' as const },
  { to: '/x/none', label: 'Nothing', count: 0, current: false },
  { to: '/x/flag', label: 'Flag', attention: true, current: false },
]

describe('ScopeSegment', () => {
  it('is a named navigation whose links announce their counts and mark the current scope', async () => {
    const { container } = render(<MemoryRouter><ScopeSegment label="Example scope" items={items} /></MemoryRouter>)
    expect(screen.getByRole('navigation', { name: 'Example scope' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Map (6)' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Inbox (9)' })).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('link', { name: 'Inbox (9)' })).toHaveAttribute('href', '/x/inbox')
    // A zero scope is bare, not "Nothing (0)".
    expect(screen.getByRole('link', { name: 'Nothing' })).toBeInTheDocument()
    // A yes/no signal is a dot and "needs attention", never a count of one.
    expect(screen.getByRole('link', { name: 'Flag (needs attention)' })).toBeInTheDocument()
    expect(await axe(container)).toHaveNoViolations()
  })
})
