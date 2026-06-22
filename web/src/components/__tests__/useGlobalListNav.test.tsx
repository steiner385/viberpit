import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useGlobalListNav } from '../useGlobalListNav'

function Harness(props: { enabled: boolean; onNavigate: (d: 'up' | 'down') => void; withDialog?: boolean; withInput?: boolean }) {
  useGlobalListNav({ enabled: props.enabled, onNavigate: props.onNavigate })
  return (
    <div>
      <button>focusable</button>
      {props.withInput && <input aria-label="field" />}
      {props.withDialog && <div role="dialog">modal</div>}
    </div>
  )
}

describe('useGlobalListNav', () => {
  it('navigates up/down when enabled and focus is on a non-input element', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<Harness enabled onNavigate={onNavigate} />)
    await user.click(screen.getByRole('button', { name: /focusable/i }))
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).toHaveBeenCalledWith('down')
    await user.keyboard('{ArrowUp}')
    expect(onNavigate).toHaveBeenCalledWith('up')
  })

  it('does nothing when disabled', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<Harness enabled={false} onNavigate={onNavigate} />)
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).not.toHaveBeenCalled()
  })

  it('bails when focus is inside an input', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<Harness enabled withInput onNavigate={onNavigate} />)
    await user.click(screen.getByLabelText('field'))
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).not.toHaveBeenCalled()
  })

  it('bails when a dialog is open', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<Harness enabled withDialog onNavigate={onNavigate} />)
    await user.click(screen.getByRole('button', { name: /focusable/i }))
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).not.toHaveBeenCalled()
  })

  it('bails when the event default is already prevented', () => {
    const onNavigate = vi.fn()
    render(<Harness enabled onNavigate={onNavigate} />)
    screen.getByRole('button', { name: /focusable/i }).focus()
    const ev = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true })
    ev.preventDefault()
    document.dispatchEvent(ev)
    expect(onNavigate).not.toHaveBeenCalled()
  })
})
