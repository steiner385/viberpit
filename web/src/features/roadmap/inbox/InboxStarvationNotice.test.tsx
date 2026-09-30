import { cleanup, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
const useHolds = vi.fn()
vi.mock('@/api/hooks', () => ({ useHolds: (...a: unknown[]) => useHolds(...a) }))
import { InboxStarvationNotice } from './InboxStarvationNotice'

const product = (slug: string, starving: boolean, dispatchable = 0, heldCount = 12) =>
  ({ slug, starving, dispatchable, heldCount, skipped: {}, oldestHoldHours: null })

it('renders ONE banner: a line per starving product (max 3), +N more, and nothing when none', () => {
  useHolds.mockReturnValue({ data: { products: [product('kindash', true, 0, 12), product('viberpit', false, 9, 1),
                                                 product('a', true), product('b', true), product('c', true), product('d', true)] } })
  const { container } = render(<InboxStarvationNotice product={null} />)
  const status = screen.getByRole('status')
  expect(container.querySelectorAll('[role="status"]')).toHaveLength(1)
  expect(status).toHaveTextContent('kindash: 0 dispatchable, 12 held — the runner is waiting on you')
  expect(status).toHaveTextContent('a: 0 dispatchable, 12 held')
  expect(status).toHaveTextContent('b: 0 dispatchable, 12 held')
  expect(status).not.toHaveTextContent('c: 0 dispatchable')
  expect(status).toHaveTextContent('+2 more')
  expect(document.body.textContent).not.toContain('viberpit')
  cleanup()
  useHolds.mockReturnValue({ data: { products: [product('kindash', false, 5, 0)] } })
  const { container: empty } = render(<InboxStarvationNotice product="kindash" />)
  expect(empty.querySelectorAll('[role="status"]')).toHaveLength(0)
})

it('passes the product chip through to the hook and tolerates no data', () => {
  useHolds.mockReturnValue({ data: undefined })
  const { container } = render(<InboxStarvationNotice product="kindash" />)
  expect(useHolds).toHaveBeenLastCalledWith('kindash', expect.anything())
  expect(container).toBeEmptyDOMElement()
})
