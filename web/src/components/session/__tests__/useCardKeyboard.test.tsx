import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useCardKeyboard, type CardKeyboardOptions } from '../useCardKeyboard'

function Harness(opts: CardKeyboardOptions & { withInput?: boolean }) {
  const kb = useCardKeyboard(opts)
  return (
    <div data-testid="container" {...kb.containerProps}>
      {opts.withInput && <input aria-label="freeform" />}
      {Array.from({ length: opts.count }).map((_, i) => (
        <button key={i} {...kb.itemProps(i)} data-testid={`item-${i}`}>opt{i}</button>
      ))}
      <span data-testid="active">{kb.activeIndex}</span>
    </div>
  )
}

describe('useCardKeyboard', () => {
  it('focuses item 0 on mount', () => {
    render(<Harness count={3} onActivate={vi.fn()} />)
    expect(screen.getByTestId('item-0')).toHaveFocus()
  })

  it('auto-focus never scrolls (preventScroll), so a carousel card cannot yank the shared track', () => {
    // Root cause of the broken carousel navigation: focusing an option makes the
    // browser scroll the nearest scrollable ancestor (the snap track) to reveal
    // it. Mid-navigation, onScroll briefly resolves back to the old card, whose
    // autoFocus flips false→true, refocuses, and scrolls the track back — a
    // feedback loop that pins the carousel to question 1. Focus must therefore
    // never scroll: both the mount focus and the becomes-active refocus pass
    // { preventScroll: true }.
    const focusSpy = vi.spyOn(HTMLElement.prototype, 'focus')
    const { rerender } = render(<Harness count={3} autoFocus={false} onActivate={vi.fn()} />)
    expect(focusSpy).not.toHaveBeenCalled()
    rerender(<Harness count={3} autoFocus onActivate={vi.fn()} />) // card becomes active
    expect(screen.getByTestId('item-0')).toHaveFocus()
    expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true })
    focusSpy.mockRestore()
  })

  it('ArrowDown/ArrowUp move the highlight with wraparound', async () => {
    const user = userEvent.setup()
    render(<Harness count={3} onActivate={vi.fn()} />)
    await user.keyboard('{ArrowDown}')
    expect(screen.getByTestId('active')).toHaveTextContent('1')
    await user.keyboard('{ArrowUp}{ArrowUp}') // 1 -> 0 -> wrap to 2
    expect(screen.getByTestId('active')).toHaveTextContent('2')
  })

  it('digit key activates that option (single-select)', async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    render(<Harness count={3} onActivate={onActivate} />)
    await user.keyboard('2')
    expect(onActivate).toHaveBeenCalledWith(1)
  })

  it('ignores a digit greater than count', async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    render(<Harness count={2} onActivate={onActivate} />)
    await user.keyboard('5')
    expect(onActivate).not.toHaveBeenCalled()
  })

  it('Enter activates the highlighted option (single-select)', async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    render(<Harness count={3} onActivate={onActivate} />)
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onActivate).toHaveBeenCalledWith(1)
  })

  it('multi-select: digit/Space toggle, Enter submits', async () => {
    const user = userEvent.setup()
    const onToggle = vi.fn()
    const onSubmit = vi.fn()
    render(<Harness count={3} multiSelect onActivate={vi.fn()} onToggle={onToggle} onSubmit={onSubmit} />)
    await user.keyboard('3')            // toggle index 2
    expect(onToggle).toHaveBeenCalledWith(2)
    await user.keyboard(' ')            // toggle highlighted (0)
    expect(onToggle).toHaveBeenCalledWith(0)
    await user.keyboard('{Enter}')
    expect(onSubmit).toHaveBeenCalledOnce()
  })

  it('bails when the event target is an INPUT', async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    render(<Harness count={3} withInput onActivate={onActivate} />)
    await user.click(screen.getByLabelText('freeform'))
    await user.keyboard('2')
    expect(onActivate).not.toHaveBeenCalled()
    expect(screen.getByLabelText('freeform')).toHaveValue('2')
  })
})
