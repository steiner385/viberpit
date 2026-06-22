import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SidebarResizer } from '../SidebarResizer'
import { SIDEBAR_MIN, SIDEBAR_MAX } from '@/lib/sidebarWidth'

function renderResizer(width = 320) {
  const onResize = vi.fn()
  render(<SidebarResizer width={width} onResize={onResize} />)
  return { onResize, handle: screen.getByRole('separator', { name: 'Resize sidebar' }) }
}

describe('SidebarResizer', () => {
  it('exposes ARIA separator semantics with current width', () => {
    const { handle } = renderResizer(360)
    expect(handle).toHaveAttribute('aria-orientation', 'vertical')
    expect(handle).toHaveAttribute('aria-valuenow', '360')
    expect(handle).toHaveAttribute('aria-valuemin', String(SIDEBAR_MIN))
    expect(handle).toHaveAttribute('aria-valuemax', String(SIDEBAR_MAX))
    expect(handle).toHaveAttribute('tabindex', '0')
  })

  it('nudges wider on ArrowRight and narrower on ArrowLeft', () => {
    const { onResize, handle } = renderResizer(320)
    fireEvent.keyDown(handle, { key: 'ArrowRight' })
    expect(onResize).toHaveBeenCalledWith(336)
    fireEvent.keyDown(handle, { key: 'ArrowLeft' })
    expect(onResize).toHaveBeenCalledWith(304)
  })

  it('jumps to min/max on Home/End', () => {
    const { onResize, handle } = renderResizer(400)
    fireEvent.keyDown(handle, { key: 'Home' })
    expect(onResize).toHaveBeenCalledWith(SIDEBAR_MIN)
    fireEvent.keyDown(handle, { key: 'End' })
    expect(onResize).toHaveBeenCalledWith(SIDEBAR_MAX)
  })

  it('resizes on pointer drag', () => {
    const { onResize, handle } = renderResizer(320)
    fireEvent.pointerDown(handle, { clientX: 100 })
    fireEvent.pointerMove(window, { clientX: 150 })
    expect(onResize).toHaveBeenCalledWith(370)
    fireEvent.pointerUp(window)
    // After release, further movement is ignored.
    onResize.mockClear()
    fireEvent.pointerMove(window, { clientX: 250 })
    expect(onResize).not.toHaveBeenCalled()
  })
})
