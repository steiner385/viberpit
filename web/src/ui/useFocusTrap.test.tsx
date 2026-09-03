import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useRef, useState } from 'react'
import { useFocusTrap } from './useFocusTrap'

function Host({ onClose }: { onClose: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useFocusTrap(ref, open, { onClose: () => { setOpen(false); onClose() } })
  return (
    <div id="root">
      <button onClick={() => setOpen(true)}>open</button>
      {open && (
        <div ref={ref} tabIndex={-1}>
          <button>first</button>
          <button>last</button>
        </div>
      )}
    </div>
  )
}

function NestedHost({ onOuterClose, onInnerClose }: { onOuterClose?: () => void; onInnerClose?: () => void } = {}) {
  const [outerOpen, setOuterOpen] = useState(false)
  const [innerOpen, setInnerOpen] = useState(false)
  const outerRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  useFocusTrap(outerRef, outerOpen, { onClose: () => { setOuterOpen(false); onOuterClose?.() } })
  useFocusTrap(innerRef, innerOpen, { onClose: () => { setInnerOpen(false); onInnerClose?.() } })
  return (
    <div id="root">
      <button onClick={() => setOuterOpen(true)}>open-outer</button>
      {outerOpen && (
        <div ref={outerRef} tabIndex={-1}>
          <button onClick={() => setInnerOpen(true)}>open-inner</button>
          <button onClick={() => setOuterOpen(false)}>close-outer</button>
          {innerOpen && (
            <div ref={innerRef} tabIndex={-1}>
              <button onClick={() => setInnerOpen(false)}>close-inner</button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

describe('useFocusTrap', () => {
  it('moves focus in, sets inert on #root, wraps Tab, restores focus and clears inert on close', () => {
    let closed = 0
    render(<Host onClose={() => { closed++ }} />)
    const opener = screen.getByText('open')
    opener.focus()
    fireEvent.click(opener)
    const root = document.getElementById('root')!
    expect(document.activeElement).toBe(screen.getByText('first'))
    expect(root.hasAttribute('inert')).toBe(true)
    screen.getByText('last').focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByText('first'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(closed).toBe(1)
    expect(root.hasAttribute('inert')).toBe(false)
    expect(document.activeElement).toBe(opener)
  })

  it('keeps #root inert while a nested trap is stacked, and only clears it once the outer trap closes', () => {
    render(<NestedHost />)
    const root = document.getElementById('root')!

    fireEvent.click(screen.getByText('open-outer'))
    expect(root.hasAttribute('inert')).toBe(true)

    fireEvent.click(screen.getByText('open-inner'))
    expect(root.hasAttribute('inert')).toBe(true)

    // Closing the inner (nested) trap must NOT lift inert — the outer trap is
    // still active. A boolean (rather than depth-counted) implementation would
    // fail this assertion by removing the attribute here.
    fireEvent.click(screen.getByText('close-inner'))
    expect(root.hasAttribute('inert')).toBe(true)

    fireEvent.click(screen.getByText('close-outer'))
    expect(root.hasAttribute('inert')).toBe(false)
  })

  it('one Escape closes only the innermost stacked trap', () => {
    // Every active trap installs its own `document` keydown listener.
    // `stopPropagation()` stops other *nodes*, not sibling listeners on the same
    // node, so one Escape used to invoke both `onClose` callbacks — and because
    // listeners fire in registration order (outermost first), the outer trap is
    // the one that gets to swallow the event. Only the trap on top of the stack
    // may act on Escape.
    const outer = vi.fn()
    const inner = vi.fn()
    render(<NestedHost onOuterClose={outer} onInnerClose={inner} />)
    fireEvent.click(screen.getByText('open-outer'))
    fireEvent.click(screen.getByText('open-inner'))

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(inner).toHaveBeenCalledTimes(1)
    expect(outer).not.toHaveBeenCalled()
    expect(screen.queryByText('close-inner')).toBeNull()
    expect(screen.getByText('close-outer')).toBeInTheDocument()

    // With the inner trap gone the outer is back on top and takes the next one.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(outer).toHaveBeenCalledTimes(1)
    expect(inner).toHaveBeenCalledTimes(1)
  })
})
