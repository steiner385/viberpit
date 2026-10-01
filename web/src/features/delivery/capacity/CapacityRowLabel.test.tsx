import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { CapacityRowLabel } from './CapacityRowLabel'
import type { CapacityStackRow } from './capacityStack'

const PRODUCTS: CapacityStackRow = {
  key: 'products', label: 'Products', used: 1, cap: 4, free: 3, href: '#products', binding: false,
}

function Probe() {
  const { pathname, hash } = useLocation()
  return <div data-testid="url">{pathname + hash}</div>
}

function mount() {
  return render(
    <MemoryRouter initialEntries={['/delivery?scope=capacity']}>
      <CapacityRowLabel row={PRODUCTS} />
      <Probe />
    </MemoryRouter>,
  )
}

afterEach(() => { document.getElementById('products')?.remove() })

describe('CapacityRowLabel', () => {
  it('scrolls to an anchor target that is on screen (the pane beside the list on desktop)', () => {
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
    const target = document.createElement('div')
    target.id = 'products'
    document.body.appendChild(target)
    mount()
    fireEvent.click(screen.getByRole('link', { name: 'Products' }))
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('url')).toHaveTextContent('/delivery')
  })

  // 2026-10-01 Delivery re-critique, P0: below desktop the pane is not mounted
  // while the list has the band, so the same anchor used to do nothing.
  it('opens the Capacity pane as an item, with the hash, when the target is not on screen', () => {
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
    mount()
    fireEvent.click(screen.getByRole('link', { name: 'Products' }))
    expect(scrollIntoView).not.toHaveBeenCalled()
    expect(screen.getByTestId('url')).toHaveTextContent('/delivery/capacity/governor#products')
  })
})
