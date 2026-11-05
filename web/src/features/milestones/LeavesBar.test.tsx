import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LeavesBar } from './LeavesBar'
import { FRIENDLY, HOUSEHOLD } from './__fixtures__/ladder'

describe('LeavesBar', () => {
  it('counts done of total and in flight', () => {
    render(<LeavesBar leaves={HOUSEHOLD.leaves} />)
    expect(screen.getByText('17 of 18 done · 1 in flight')).toBeInTheDocument()
  })

  it('adds not started when there are any', () => {
    render(<LeavesBar leaves={FRIENDLY.leaves} />)
    expect(screen.getByText('0 of 9 done · 1 in flight · 8 not started')).toBeInTheDocument()
  })

  it('draws the segments in proportion, hidden from assistive tech', () => {
    const { container } = render(<LeavesBar leaves={HOUSEHOLD.leaves} />)
    const bar = container.querySelector('[data-leaves-bar]')!
    expect(bar).toHaveAttribute('aria-hidden', 'true')
    const done = bar.querySelector('[data-segment="done"]') as HTMLElement
    expect(done.style.width).toBe(`${(17 / 18) * 100}%`)
  })
})
