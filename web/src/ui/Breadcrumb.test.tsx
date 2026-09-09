import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Breadcrumb } from './Breadcrumb'

describe('Breadcrumb', () => {
  it('links every item but the last and marks the last as current', () => {
    render(
      <MemoryRouter>
        <Breadcrumb items={[{ label: 'Roadmap', to: '/roadmap' }, { label: 'KinDash', to: '/roadmap/kindash' }, { label: 'Tasks' }]} />
      </MemoryRouter>,
    )
    const nav = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(nav).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Roadmap' })).toHaveAttribute('href', '/roadmap')
    expect(screen.getByRole('link', { name: 'KinDash' })).toHaveAttribute('href', '/roadmap/kindash')
    expect(screen.queryByRole('link', { name: 'Tasks' })).toBeNull()
    expect(screen.getByText('Tasks')).toHaveAttribute('aria-current', 'page')
  })
})
