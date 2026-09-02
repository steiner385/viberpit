import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Banner } from './Banner'
import { BannerStack } from './BannerStack'

expect.extend(toHaveNoViolations)

describe('Banner', () => {
  it('danger is an alert; warning is a status; dismiss calls back', async () => {
    const onDismiss = vi.fn()
    const { container } = render(
      <>
        <Banner tone="danger" title="Daemon down">restart it</Banner>
        <Banner tone="warning" title="All accounts at limit" onDismiss={onDismiss} />
      </>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Daemon down')
    expect(screen.getByRole('status')).toHaveTextContent('All accounts at limit')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(onDismiss).toHaveBeenCalled()
    expect(await axe(container)).toHaveNoViolations()
  })
})

describe('BannerStack', () => {
  it('orders by priority, skips null, mounts once', () => {
    render(<BannerStack items={[
      { id: 'low', priority: 1, node: <Banner tone="info" title="Low" /> },
      { id: 'none', priority: 5, node: null },
      { id: 'high', priority: 9, node: <Banner tone="danger" title="High" /> },
    ]} />)
    const titles = screen.getAllByText(/High|Low/).map((el) => el.textContent)
    expect(titles).toEqual(['High', 'Low'])
    expect(document.querySelectorAll('[data-region="app.banners"]').length).toBe(1)
  })
})
