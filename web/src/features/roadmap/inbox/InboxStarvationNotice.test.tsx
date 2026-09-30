import { render, screen } from '@testing-library/react'
import { vi } from 'vitest'
const useHolds = vi.fn()
vi.mock('@/api/hooks', () => ({ useHolds: (...a: unknown[]) => useHolds(...a) }))
import { InboxStarvationNotice } from './InboxStarvationNotice'

const product = (slug: string, starving: boolean, dispatchable = 0, heldCount = 12) =>
  ({ slug, starving, dispatchable, heldCount, skipped: {}, oldestHoldHours: null })

it('renders one line per starving product (max 3), and nothing when none is starving', () => {
  useHolds.mockReturnValue({ data: { products: [product('kindash', true, 0, 12), product('viberpit', false, 9, 1),
                                                 product('a', true), product('b', true), product('c', true)] } })
  render(<InboxStarvationNotice product={null} />)
  const status = screen.getAllByRole('status')[0]
  expect(status).toHaveTextContent('kindash: 0 dispatchable, 12 held — the runner is waiting on you')
  expect(document.body.textContent).not.toContain('viberpit')
  expect(screen.getAllByText(/waiting on you/)).toHaveLength(3)
  useHolds.mockReturnValue({ data: { products: [product('kindash', false, 5, 0)] } })
  const { container } = render(<InboxStarvationNotice product="kindash" />)
  expect(container.querySelectorAll('[role="status"]')).toHaveLength(0)
})

it('passes the product chip through to the hook and tolerates no data', () => {
  useHolds.mockReturnValue({ data: undefined })
  const { container } = render(<InboxStarvationNotice product="kindash" />)
  expect(useHolds).toHaveBeenLastCalledWith('kindash', expect.anything())
  expect(container).toBeEmptyDOMElement()
})
