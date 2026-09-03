import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PageHeader } from './PageHeader'
import { PageContent } from './PageContent'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'

describe('page primitives', () => {
  it('PageHeader renders the h1 and sets document.title', () => {
    render(<PageHeader title="Automations" actions={<button>New</button>} />)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Automations')
    expect(document.title).toBe('Automations — ccswitch')
  })
  it('PageContent exposes busy state and Skeleton is decorative', () => {
    const { rerender } = render(<PageContent busy><Skeleton lines={3} /></PageContent>)
    expect(screen.getByTestId('page-content')).toHaveAttribute('aria-busy', 'true')
    expect(document.querySelector('[aria-hidden="true"] .animate-pulse')).not.toBeNull()
    rerender(<PageContent><EmptyState title="No automations yet" /></PageContent>)
    expect(screen.getByRole('status')).toHaveTextContent('Loaded')
    expect(screen.getByText('No automations yet')).toBeInTheDocument()
  })
})
