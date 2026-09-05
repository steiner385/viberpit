import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CardFrame } from '../CardFrame'

function props(over = {}) {
  return {
    id: 's1',
    openLabel: 'Open session Alpha',
    railClass: 'bg-success',
    selected: false,
    listMode: false,
    onOpen: vi.fn(),
    identity: <span>Alpha</span>,
    ...over,
  }
}

function fg() {
  const el = screen.getByTestId('card-swipe-fg')
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: 300 }) // commit at 135px
  return el
}

describe('CardFrame', () => {
  it('paints the rail with the class it is given', () => {
    render(<CardFrame {...props()} />)
    expect(screen.getByTestId('card-status-rail').className).toContain('bg-success')
  })

  it('opens from the open control', () => {
    const onOpen = vi.fn()
    render(<CardFrame {...props({ onOpen })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open session Alpha' }))
    expect(onOpen).toHaveBeenCalledWith('s1')
  })

  it('opens from a click on passive card area, but not from a click on a trailing button', () => {
    const onOpen = vi.fn()
    const onAction = vi.fn()
    render(
      <CardFrame
        {...props({ onOpen })}
        trailing={<button type="button" onClick={onAction}>act</button>}
      >
        <div data-testid="line2">context</div>
      </CardFrame>,
    )
    fireEvent.click(screen.getByTestId('line2'))
    expect(onOpen).toHaveBeenCalledWith('s1')
    onOpen.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'act' }))
    expect(onAction).toHaveBeenCalledOnce()
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('commits the right-swipe action', () => {
    const onCommit = vi.fn()
    render(
      <CardFrame
        {...props()}
        swipeRight={{ label: 'Archive Alpha', icon: <i />, onCommit, testId: 'swipe-panel-archive', className: 'bg-gray-200' }}
      />,
    )
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 50, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 210, clientY: 45 })   // dx +160
    fireEvent.pointerUp(window, { clientX: 210, clientY: 45 })
    expect(onCommit).toHaveBeenCalledOnce()
  })

  it('reveals a tappable button on a short left-swipe rather than committing', () => {
    const onCommit = vi.fn()
    render(
      <CardFrame
        {...props()}
        swipeLeft={{ label: 'Delete Alpha', icon: <i />, onCommit, testId: 'swipe-panel-delete', className: 'bg-red-600' }}
      />,
    )
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 170, clientY: 42 })   // dx -80: reveal only
    fireEvent.pointerUp(window, { clientX: 170, clientY: 42 })
    expect(onCommit).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Delete Alpha' }))
    expect(onCommit).toHaveBeenCalledOnce()
  })

  it('mounts no swipe edge that was not configured', () => {
    render(<CardFrame {...props()} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 170, clientY: 42 })
    fireEvent.pointerUp(window, { clientX: 170, clientY: 42 })
    expect(screen.queryByTestId('swipe-panel-delete')).not.toBeInTheDocument()
  })

  it('does not translate or clip on a sub-commit drag toward an edge with no action configured', () => {
    // Only swipeRight is wired; drag toward the (unconfigured) left edge far
    // enough to reveal (>64px) but not commit (<135px of a 300px card).
    // settleSwipe still returns {outcome:'open', side:'left'} — CardFrame must
    // not act on that reveal for an edge with no action, or the foreground
    // stays translated 88px with nothing rendered behind it.
    render(
      <CardFrame
        {...props()}
        swipeRight={{ label: 'Unarchive Alpha', icon: <i />, onCommit: vi.fn(), testId: 'swipe-panel-unarchive', className: 'bg-blue-600' }}
      />,
    )
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 170, clientY: 42 })   // dx -80: sub-commit toward the unconfigured LEFT edge
    fireEvent.pointerUp(window, { clientX: 170, clientY: 42 })
    expect(el.style.transform).toBe('')
    expect(el.parentElement?.className).not.toContain('overflow-hidden')
  })

  it('opens on Enter when not in list mode, and defers keys to onKeyDown when it is', () => {
    const onOpen = vi.fn()
    const onKeyDown = vi.fn()
    const { rerender } = render(<CardFrame {...props({ onOpen })} />)
    fireEvent.keyDown(screen.getByRole('button', { name: 'Open session Alpha' }), { key: 'Enter' })
    expect(onOpen).toHaveBeenCalledWith('s1')

    onOpen.mockClear()
    rerender(<CardFrame {...props({ onOpen, listMode: true, onKeyDown })} />)
    fireEvent.keyDown(screen.getByRole('button', { name: 'Open session Alpha' }), { key: 'x' })
    expect(onKeyDown).toHaveBeenCalledOnce()
  })

  it('gives the selected row the focusable tabindex and the ring', () => {
    render(<CardFrame {...props({ selected: true })} />)
    expect(screen.getByRole('button', { name: 'Open session Alpha' })).toHaveAttribute('tabindex', '0')
  })
})
