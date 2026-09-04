import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Banner } from './Banner'
import { BannerStack } from './BannerStack'

// Scope note: axe in jsdom covers roles, names and ARIA wiring only. No
// stylesheet is applied here, so axe's `color-contrast` rule always returns
// `incomplete`, and `toHaveNoViolations()` inspects `violations` alone — these
// assertions therefore cannot fail on colour. Contrast is covered instead by
// src/__tests__/contrast.test.ts, which does the WCAG maths on index.css.
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

  it('ignores item.tone entirely — it is metadata for the shell, not a rendering input', () => {
    // tone is optional and BannerStack does not read it: order and rendering are
    // unaffected regardless of what a caller sets it to (or omits it).
    render(<BannerStack items={[
      { id: 'a', priority: 1, tone: 'danger', node: <Banner tone="info" title="A" /> },
      { id: 'b', priority: 2, node: <Banner tone="info" title="B" /> },
    ]} />)
    expect(screen.getAllByText(/A|B/).map((el) => el.textContent)).toEqual(['B', 'A'])
  })

  it('renders an empty wrapper when all items render null internally', () => {
    // Simulate items whose components render null (e.g., DaemonRestartBanner when idle).
    // The item.node is not null, but the component function returns null. jsdom does not
    // apply CSS, so it cannot evaluate [&:empty]:hidden; we test the structural precondition:
    // the wrapper exists and has zero child nodes. The visual collapse is delivered by the
    // CSS rule, which the browser applies but jsdom does not.
    const InactiveBanner = () => null
    const { container } = render(<BannerStack items={[
      { id: 'a', priority: 1, node: <InactiveBanner /> },
      { id: 'b', priority: 2, node: <InactiveBanner /> },
    ]} />)
    const wrapper = container.querySelector('[data-region="app.banners"]')
    expect(wrapper).not.toBeNull()
    expect(wrapper!.childNodes.length).toBe(0)
    // …and the class that collapses it. jsdom does not evaluate CSS, so this
    // pins the mechanism, not the rendered height: without `[&:empty]:hidden`
    // the empty wrapper leaves an 8px strip on every screen (ruling R26), and
    // nothing else in the suite notices if the class is deleted.
    expect(wrapper!.className).toContain('[&:empty]:hidden')
  })
})
