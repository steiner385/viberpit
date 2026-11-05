import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MilestonesSummaryBlock } from './MilestonesSummaryBlock'
import { ladderView } from './__fixtures__/ladder'

const q = vi.hoisted(() => ({ data: undefined as unknown }))
vi.mock('@/api/milestones', () => ({ useMilestones: () => q }))

const mount = () => render(<MemoryRouter><MilestonesSummaryBlock productSlug="kindash" /></MemoryRouter>)

describe('MilestonesSummaryBlock', () => {
  it('is hidden when the product has no ladder', () => {
    q.data = { ladder: null }
    const { container } = mount()
    expect(container).toBeEmptyDOMElement()
  })

  it('is hidden while loading, or on an unexpected payload', () => {
    q.data = undefined
    expect(mount().container).toBeEmptyDOMElement()
  })

  it('names the current milestone and its gate, linking to the page', () => {
    q.data = ladderView()
    mount()
    const link = screen.getByRole('link', { name: /Milestones/ })
    expect(link).toHaveAttribute('href', '/roadmap/kindash/milestones')
    expect(link.closest('p')).toHaveTextContent('Milestones: Phase 0 · Household gate · 1 of 4 gate conditions met · 1 unknown')
  })
})
