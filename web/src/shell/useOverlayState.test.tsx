import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { useRef, useState } from 'react'
import {
  pushOverlay,
  popOverlay,
  overlayIsOpen,
  useOverlayState,
  resetOverlayState,
} from './useOverlayState'
import { Dialog } from '@/ui/Dialog'
import { Sheet } from '@/ui/Sheet'
import { Menu, type MenuItem } from '@/ui/Menu'

// The counter is module state, so a test that leaves it non-zero would make
// every later test read "overlay open" for free. Reset after each — and note
// that this runs AFTER @testing-library's auto-cleanup (registered at import
// time, so first), which means the unmount-balance assertions below still see
// the real post-unmount depth rather than a reset one.
afterEach(() => {
  resetOverlayState()
})

describe('overlay depth counter', () => {
  it('starts closed', () => {
    expect(overlayIsOpen()).toBe(false)
  })

  it('push opens it and a balanced pop closes it again', () => {
    pushOverlay()
    expect(overlayIsOpen()).toBe(true)
    popOverlay()
    expect(overlayIsOpen()).toBe(false)
  })

  it('nests: two pushes need two pops', () => {
    pushOverlay()
    pushOverlay()
    expect(overlayIsOpen()).toBe(true)
    popOverlay()
    // Still open — the outer overlay (e.g. a Dialog that opened a second
    // Dialog) has not closed yet.
    expect(overlayIsOpen()).toBe(true)
    popOverlay()
    expect(overlayIsOpen()).toBe(false)
  })

  it('does not underflow: an extra pop cannot poison the next push', () => {
    pushOverlay()
    popOverlay()
    popOverlay() // one pop too many
    expect(overlayIsOpen()).toBe(false)
    // The real damage of a negative counter is here, not above: at -1 this
    // push would land on 0 and the overlay would be invisible to every
    // shortcut, permanently.
    pushOverlay()
    expect(overlayIsOpen()).toBe(true)
  })

  it('resetOverlayState clears a leaked push', () => {
    pushOverlay()
    pushOverlay()
    resetOverlayState()
    expect(overlayIsOpen()).toBe(false)
  })
})

describe('useOverlayState', () => {
  function Probe() {
    const open = useOverlayState()
    return <span data-testid="probe">{open ? 'open' : 'closed'}</span>
  }

  it('re-renders a subscriber when the depth crosses zero', () => {
    render(<Probe />)
    expect(screen.getByTestId('probe')).toHaveTextContent('closed')
    act(() => { pushOverlay() })
    expect(screen.getByTestId('probe')).toHaveTextContent('open')
    act(() => { popOverlay() })
    expect(screen.getByTestId('probe')).toHaveTextContent('closed')
  })

  it('unsubscribes on unmount — a later push does not touch a dead component', () => {
    const { unmount } = render(<Probe />)
    unmount()
    // No React act() warning and no throw: the store dropped the listener.
    expect(() => { pushOverlay() }).not.toThrow()
    expect(overlayIsOpen()).toBe(true)
  })
})

// --- The primitives that must register ------------------------------------

function DialogHost() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button onClick={() => setOpen(true)}>Open dialog</button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Rename session">
        body
      </Dialog>
    </>
  )
}

const ITEMS: MenuItem[] = [
  { id: 'manual', label: 'Manual' },
  { id: 'plan', label: 'Plan' },
]

function MenuHost({ mounted = true }: { mounted?: boolean }) {
  const ref = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  return (
    <>
      <button ref={ref} onClick={() => setOpen(true)}>Mode</button>
      {mounted && (
        <Menu
          open={open}
          onClose={() => setOpen(false)}
          anchorRef={ref}
          label="Permission mode"
          onSelect={() => {}}
          items={ITEMS}
        />
      )}
    </>
  )
}

function SheetHost({ mode }: { mode: 'overlay' | 'push' }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button onClick={() => setOpen(true)}>Open sheet</button>
      <Sheet open={open} onClose={() => setOpen(false)} title="Assistant" mode={mode}>
        body
      </Sheet>
    </>
  )
}

describe('primitives register their openness', () => {
  it('a modal Dialog counts as an overlay while it is open', () => {
    render(<DialogHost />)
    expect(overlayIsOpen()).toBe(false)
    fireEvent.click(screen.getByText('Open dialog'))
    expect(overlayIsOpen()).toBe(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(overlayIsOpen()).toBe(false)
  })

  // The case NONE of the three mechanisms this module replaces could see:
  // `Menu` is not a focus trap and never rendered a `[role="dialog"]`, so both
  // DOM queries and the trap stack said "no overlay" with a menu open over the
  // session list — and ArrowUp went to the list underneath it.
  it('an open Menu counts as an overlay, and stops counting when it closes', () => {
    render(<MenuHost />)
    expect(overlayIsOpen()).toBe(false)
    fireEvent.click(screen.getByText('Mode'))
    expect(screen.getByRole('menu')).toBeInTheDocument()
    expect(overlayIsOpen()).toBe(true)
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(overlayIsOpen()).toBe(false)
  })

  it('a Menu unmounted while still open releases its push', () => {
    const { rerender } = render(<MenuHost />)
    fireEvent.click(screen.getByText('Mode'))
    expect(overlayIsOpen()).toBe(true)
    // Unmount the Menu without ever setting `open` to false — the shape that
    // leaks a push and leaves the app permanently "overlay open".
    rerender(<MenuHost mounted={false} />)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(overlayIsOpen()).toBe(false)
  })

  it('an overlay-mode Sheet counts as an overlay', () => {
    render(<SheetHost mode="overlay" />)
    fireEvent.click(screen.getByText('Open sheet'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(overlayIsOpen()).toBe(true)
  })

  // RULING R5, pinned: `overlayOpen` means modal traps + open menus. A PUSHED
  // Sheet — the drawer docked beside the content at >=1280 — is a REGION, not
  // an overlay: it sits next to the list instead of covering it, so global list
  // navigation must keep working with it open.
  //
  // This is a deliberate NARROWING of what shipped: `useGlobalListNav` queried
  // the loose `[role="dialog"]`, which a push-mode Sheet still renders (it drops
  // `aria-modal`, not the role), so having the assistant docked open used to
  // kill ArrowUp/ArrowDown for the whole app. It no longer does.
  it('a PUSHED (docked, non-modal) drawer is NOT an overlay — deliberate narrowing of useGlobalListNav', () => {
    render(<SheetHost mode="push" />)
    fireEvent.click(screen.getByText('Open sheet'))
    // It is on screen, and it still carries role="dialog" — the exact thing the
    // old query matched — but it is not modal, so it does not block shortcuts.
    const panel = screen.getByRole('dialog')
    expect(panel).toBeInTheDocument()
    expect(panel.getAttribute('aria-modal')).toBeNull()
    expect(overlayIsOpen()).toBe(false)
  })
})
