import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { SwipeSide } from '../swipeMath'
import { useSwipeAction } from '../useSwipeAction'

function Probe({ onCommit, enabled = true }: { onCommit: (s: SwipeSide) => void; enabled?: boolean }) {
  const swipe = useSwipeAction({ enabled, onCommit })
  return (
    <div data-testid="fg" onPointerDown={swipe.onPointerDown}>
      <span data-testid="open">{swipe.openSide ?? 'none'}</span>
      <span data-testid="dragging">{String(swipe.dragging)}</span>
      <button onClick={() => { if (!swipe.consumeDrag()) onCommit('right') /* stand-in for "open" */ }}>
        open
      </button>
    </div>
  )
}

/** jsdom never measures layout, so pin the card width the hook reads. */
function widthed(el: HTMLElement, px: number) {
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: px })
}

describe('useSwipeAction', () => {
  it('commits left when a horizontal drag passes the commit fraction', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300) // commit at 135px
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 60, clientY: 55 }) // dx -140, horizontal
    fireEvent.pointerUp(window, { clientX: 60, clientY: 55 })
    expect(onCommit).toHaveBeenCalledWith('left')
  })

  it('reveals (opens) a side on a short horizontal drag without committing', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300)
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 120, clientY: 52 }) // dx -80: reveal, not commit
    fireEvent.pointerUp(window, { clientX: 120, clientY: 52 })
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByTestId('open').textContent).toBe('left')
  })

  it('yields to vertical scroll: a vertical-dominant drag never engages', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300)
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 205, clientY: 200 }) // dy 150 >> dx 5
    fireEvent.pointerUp(window, { clientX: 205, clientY: 200 })
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByTestId('dragging').textContent).toBe('false')
    expect(screen.getByTestId('open').textContent).toBe('none')
  })

  it('consumeDrag suppresses the click that trails a real drag, once', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300)
    // Short horizontal drag that settles closed but DID move horizontally.
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 170, clientY: 51 }) // dx -30: closed, but a drag
    fireEvent.pointerUp(window, { clientX: 170, clientY: 51 })
    fireEvent.click(screen.getByText('open'))
    expect(onCommit).not.toHaveBeenCalled() // first click swallowed
    fireEvent.click(screen.getByText('open'))
    expect(onCommit).toHaveBeenCalledWith('right') // flag cleared; second click acts
  })

  it('does nothing when disabled', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} enabled={false} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300)
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 60, clientY: 55 })
    fireEvent.pointerUp(window, { clientX: 60, clientY: 55 })
    expect(onCommit).not.toHaveBeenCalled()
  })
})
