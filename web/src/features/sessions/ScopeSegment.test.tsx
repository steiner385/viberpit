// ScopeSegment — the sessions list's Active / Archived switch.
//
// The property under test is that it is a NAVIGATION and holds no state: each
// half is a link to the path that owns that scope, and "you are here" is
// `aria-current`, driven entirely by the `scope` prop the route produced.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { ScopeSegment } from './ScopeSegment'

function Url() {
  const { pathname } = useLocation()
  return <div data-testid="url">{pathname}</div>
}

function renderSegment(scope: 'live' | 'archived', at = '/sessions') {
  return render(
    <MemoryRouter initialEntries={[at]}>
      <ScopeSegment scope={scope} activeCount={7} archivedCount={132} />
      <Url />
    </MemoryRouter>,
  )
}

describe('ScopeSegment', () => {
  it('is a named navigation carrying both counts', () => {
    renderSegment('live')
    expect(screen.getByRole('navigation', { name: 'Session scope' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Active/ })).toHaveTextContent('7')
    expect(screen.getByRole('link', { name: /Archived/ })).toHaveTextContent('132')
  })

  it('marks the current scope with aria-current', () => {
    renderSegment('archived', '/sessions/archived')
    expect(screen.getByRole('link', { name: /Archived/ })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: /Active/ })).not.toHaveAttribute('aria-current')
  })

  it('navigates to the archived scope', async () => {
    renderSegment('live')
    await userEvent.click(screen.getByRole('link', { name: /Archived/ }))
    expect(screen.getByTestId('url').textContent).toBe('/sessions/archived')
  })

  it('navigates back to the live scope', async () => {
    // Exact, not `toHaveTextContent`: '/sessions' is a prefix of
    // '/sessions/archived', so a substring match would pass without navigating.
    renderSegment('archived', '/sessions/archived')
    await userEvent.click(screen.getByRole('link', { name: /Active/ }))
    expect(screen.getByTestId('url').textContent).toBe('/sessions')
  })

  it('omits a zero count rather than showing "0"', () => {
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <ScopeSegment scope="live" activeCount={0} archivedCount={0} />
      </MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: /Active/ }).textContent).toBe('Active')
    expect(screen.getByRole('link', { name: /Archived/ }).textContent).toBe('Archived')
  })
})
