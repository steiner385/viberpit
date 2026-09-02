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
})
