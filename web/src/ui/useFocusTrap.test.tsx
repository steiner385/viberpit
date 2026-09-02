import { describe, it, expect } from 'vitest'
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

function NestedHost() {
  const [outerOpen, setOuterOpen] = useState(false)
  const [innerOpen, setInnerOpen] = useState(false)
  const outerRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  useFocusTrap(outerRef, outerOpen, { onClose: () => setOuterOpen(false) })
  useFocusTrap(innerRef, innerOpen, { onClose: () => setInnerOpen(false) })
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
})
