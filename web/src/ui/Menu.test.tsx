import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useRef, useState } from 'react'
import { Menu, type MenuItem } from './Menu'

function Host({ onSelect, items }: { onSelect: (id: string) => void; items: MenuItem[] }) {
  const ref = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  return (
    <>
      <button ref={ref} onClick={() => setOpen(true)}>Mode</button>
      <Menu open={open} onClose={() => setOpen(false)} anchorRef={ref} label="Permission mode" onSelect={onSelect} items={items} />
    </>
  )
}

const BASE_ITEMS: MenuItem[] = [
  { id: 'manual', label: 'Manual' },
  { id: 'accept', label: 'Accept edits', disabled: true },
  { id: 'plan', label: 'Plan' },
]

// Four items, one disabled in the middle: enabled positions are Alpha(1),
// Charlie(2), Delta(3) — Bravo consumes neither a digit nor a badge number.
const FOUR_ITEMS: MenuItem[] = [
  { id: 'a', label: 'Alpha' },
  { id: 'b', label: 'Bravo', disabled: true },
  { id: 'c', label: 'Charlie' },
  { id: 'd', label: 'Delta' },
]

describe('Menu', () => {
  it('opens with focus on the first item, arrows skip disabled, digit picks the Nth enabled item', () => {
    const onSelect = vi.fn()
    render(<Host onSelect={onSelect} items={BASE_ITEMS} />)
    fireEvent.click(screen.getByText('Mode'))
    const menu = screen.getByRole('menu', { name: 'Permission mode' })
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Manual/ }))
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Plan/ }))
    fireEvent.keyDown(menu, { key: '2' })
    expect(onSelect).toHaveBeenCalledWith('plan')
  })

  it('Escape closes and returns focus to the anchor', () => {
    render(<Host onSelect={() => {}} items={BASE_ITEMS} />)
    const anchor = screen.getByText('Mode')
    fireEvent.click(anchor)
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(anchor)
  })

  it('clicking outside the menu closes it and returns focus to the anchor', () => {
    render(<Host onSelect={() => {}} items={BASE_ITEMS} />)
    const anchor = screen.getByText('Mode')
    fireEvent.click(anchor)
    expect(screen.getByRole('menu')).toBeInTheDocument()
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(anchor)
  })

  it('Home/End jump to the first/last enabled item, skipping disabled ones', () => {
    render(<Host onSelect={() => {}} items={FOUR_ITEMS} />)
    fireEvent.click(screen.getByText('Mode'))
    const menu = screen.getByRole('menu')
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Alpha/ }))
    fireEvent.keyDown(menu, { key: 'End' })
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Delta/ }))
    fireEvent.keyDown(menu, { key: 'Home' })
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Alpha/ }))
  })

  it('Enter and Space select the active item, close the menu, and return focus to the anchor', () => {
    const onSelect = vi.fn()
    render(<Host onSelect={onSelect} items={FOUR_ITEMS} />)
    const anchor = screen.getByText('Mode')
    fireEvent.click(anchor)
    let menu = screen.getByRole('menu')
    fireEvent.keyDown(menu, { key: 'ArrowDown' }) // Alpha -> Charlie
    fireEvent.keyDown(menu, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('c')
    expect(screen.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(anchor)

    onSelect.mockClear()
    fireEvent.click(anchor)
    menu = screen.getByRole('menu')
    fireEvent.keyDown(menu, { key: ' ' })
    expect(onSelect).toHaveBeenCalledWith('a')
    expect(screen.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(anchor)
  })

  it('numbers each enabled item in order and gives the disabled item no badge; a disabled item consumes no digit', () => {
    const onSelect = vi.fn()
    render(<Host onSelect={onSelect} items={FOUR_ITEMS} />)
    fireEvent.click(screen.getByText('Mode'))
    const menu = screen.getByRole('menu')

    const alpha = screen.getByRole('menuitem', { name: /Alpha/ })
    const bravo = screen.getByRole('menuitem', { name: /Bravo/ })
    const charlie = screen.getByRole('menuitem', { name: /Charlie/ })
    const delta = screen.getByRole('menuitem', { name: /Delta/ })

    // Badge numbers follow enabled-only order: Alpha=1, Charlie=2, Delta=3.
    expect(alpha).toHaveTextContent('1')
    expect(bravo.textContent).not.toMatch(/[1-9]/)
    expect(charlie).toHaveTextContent('2')
    expect(delta).toHaveTextContent('3')

    // Digit 3 selects Delta (the 3rd *enabled* item), matching its badge.
    fireEvent.keyDown(menu, { key: '3' })
    expect(onSelect).toHaveBeenCalledWith('d')
  })

  it('a digit beyond the number of enabled items selects nothing', () => {
    const onSelect = vi.fn()
    render(<Host onSelect={onSelect} items={FOUR_ITEMS} />)
    fireEvent.click(screen.getByText('Mode'))
    const menu = screen.getByRole('menu')
    // Only 3 enabled items exist; '4' must not select anything.
    fireEvent.keyDown(menu, { key: '4' })
    expect(onSelect).not.toHaveBeenCalled()
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('typeahead moves the active item to the next enabled item whose label starts with the typed letter', () => {
    const items: MenuItem[] = [
      { id: 'apple', label: 'Apple' },
      { id: 'banana', label: 'Banana' },
      { id: 'cherry', label: 'Cherry' },
    ]
    render(<Host onSelect={() => {}} items={items} />)
    fireEvent.click(screen.getByText('Mode'))
    const menu = screen.getByRole('menu')
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Apple/ }))
    fireEvent.keyDown(menu, { key: 'c' })
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Cherry/ }))
  })

  it('typeahead cycles to the next match on repeat presses of the same letter, wrapping around', () => {
    const items: MenuItem[] = [
      { id: 'apple', label: 'Apple' },
      { id: 'avocado', label: 'Avocado' },
      { id: 'banana', label: 'Banana' },
    ]
    render(<Host onSelect={() => {}} items={items} />)
    fireEvent.click(screen.getByText('Mode'))
    const menu = screen.getByRole('menu')
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /^Apple$/ }))
    // First 'a': advances past Apple (the current item) to the next 'a' item, Avocado.
    fireEvent.keyDown(menu, { key: 'a' })
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Avocado/ }))
    // Second 'a': Banana doesn't match, so it wraps back around to Apple.
    fireEvent.keyDown(menu, { key: 'a' })
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /^Apple$/ }))
  })

  it('Tab closes the menu without preventing default, and does not restore focus to the anchor', () => {
    render(<Host onSelect={() => {}} items={FOUR_ITEMS} />)
    const anchor = screen.getByText('Mode')
    fireEvent.click(anchor)
    const menu = screen.getByRole('menu')
    const notPrevented = fireEvent.keyDown(menu, { key: 'Tab' })
    expect(screen.queryByRole('menu')).toBeNull()
    // fireEvent returns false only if preventDefault() was called.
    expect(notPrevented).toBe(true)
    // jsdom does not perform the browser's native Tab focus-advancement, so
    // this cannot assert focus actually *moved on* to the next control.
    // What it CAN and must assert: the anchor-restore effect did not fire
    // and clobber wherever focus was headed — contrast with the Escape test
    // above, which asserts the opposite (focus IS on the anchor) for a
    // close that has no native focus transfer to defer to.
    expect(document.activeElement).not.toBe(anchor)
  })

  it('Shift+Tab also closes the menu without preventing default or restoring focus to the anchor', () => {
    render(<Host onSelect={() => {}} items={FOUR_ITEMS} />)
    const anchor = screen.getByText('Mode')
    fireEvent.click(anchor)
    const menu = screen.getByRole('menu')
    const notPrevented = fireEvent.keyDown(menu, { key: 'Tab', shiftKey: true })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(notPrevented).toBe(true)
    expect(document.activeElement).not.toBe(anchor)
  })

  it('marks a single-choice item as menuitemradio with aria-checked, and leaves an unselectable item as a plain menuitem with no aria-checked', () => {
    const items: MenuItem[] = [
      { id: 'sonnet', label: 'Sonnet', selected: true },
      { id: 'opus', label: 'Opus', selected: false },
    ]
    render(<Host onSelect={() => {}} items={items} />)
    fireEvent.click(screen.getByText('Mode'))
    // Plain BASE_ITEMS/FOUR_ITEMS elsewhere in this file never set `selected`
    // and are correctly queried via role "menuitem" in those tests; here every
    // item sets `selected`, so all render as menuitemradio.
    const sonnet = screen.getByRole('menuitemradio', { name: /Sonnet/ })
    const opus = screen.getByRole('menuitemradio', { name: /Opus/ })
    expect(sonnet).toHaveAttribute('aria-checked', 'true')
    expect(opus).toHaveAttribute('aria-checked', 'false')
  })

  it('a plain menuitem (no item sets `selected`) never carries aria-checked', () => {
    render(<Host onSelect={() => {}} items={BASE_ITEMS} />)
    fireEvent.click(screen.getByText('Mode'))
    const manual = screen.getByRole('menuitem', { name: /Manual/ })
    expect(manual).not.toHaveAttribute('aria-checked')
  })

  it('an unrelated parent re-render with a fresh inline onClose does not steal focus from the active menu item', () => {
    function ToggleHost() {
      const ref = useRef<HTMLButtonElement>(null)
      const [open, setOpen] = useState(false)
      const [tick, setTick] = useState(0)
      return (
        <>
          <button ref={ref} onClick={() => setOpen(true)}>Mode</button>
          <button onClick={() => setTick((t) => t + 1)}>Bump</button>
          <span>{tick}</span>
          {/* Inline onClose: a fresh function identity on every render, exactly
              like the composer pickers this primitive is built for. */}
          <Menu open={open} onClose={() => setOpen(false)} anchorRef={ref} label="Permission mode" onSelect={() => {}} items={FOUR_ITEMS} />
        </>
      )
    }
    render(<ToggleHost />)
    fireEvent.click(screen.getByText('Mode'))
    const menu = screen.getByRole('menu')
    fireEvent.keyDown(menu, { key: 'ArrowDown' }) // Alpha -> Charlie
    const charlie = screen.getByRole('menuitem', { name: /Charlie/ })
    expect(document.activeElement).toBe(charlie)

    // Unrelated re-render of the host: recreates the inline onClose passed to
    // Menu. This must not tear focus off the active menu item.
    fireEvent.click(screen.getByText('Bump'))
    expect(screen.getByRole('menu')).toBeInTheDocument()
    expect(document.activeElement).toBe(charlie)
  })
})
