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

  it('defaults every non-danger tone to a status live region', () => {
    render(
      <>
        <Banner tone="info" title="Info" />
        <Banner tone="success" title="Success" />
        <Banner tone="warning" title="Warning" />
      </>,
    )
    expect(screen.getAllByRole('status')).toHaveLength(3)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('an explicit role="alert" on a non-danger tone wins over the tone default', () => {
    render(<Banner tone="warning" role="alert" title="All accounts at limit" />)
    expect(screen.getByRole('alert')).toHaveTextContent('All accounts at limit')
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('role="none" renders no role attribute and no live region, for content unsafe to announce', () => {
    const { container } = render(<Banner tone="warning" role="none" title="Pending deploy" />)
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    const node = container.firstElementChild
    expect(node).not.toBeNull()
    expect(node).toHaveTextContent('Pending deploy')
    expect(node).not.toHaveAttribute('role')
    expect(node).not.toHaveAttribute('aria-live')
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
