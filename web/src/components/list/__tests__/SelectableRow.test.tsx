import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SelectableRow } from '../SelectableRow'

describe('SelectableRow', () => {
  it('names its checkbox after the row and reflects `checked`', () => {
    render(
      <SelectableRow id="s1" name="Work session" checked onToggle={vi.fn()}>
        <div>row body</div>
      </SelectableRow>,
    )
    const box = screen.getByRole('checkbox', { name: 'Select Work session' })
    expect(box).toBeChecked()
    expect(screen.getByText('row body')).toBeInTheDocument()
  })

  it('reports a plain click as shift=false', async () => {
    const onToggle = vi.fn()
    render(<SelectableRow id="s1" name="A" checked={false} onToggle={onToggle}>x</SelectableRow>)
    await userEvent.click(screen.getByRole('checkbox'))
    expect(onToggle).toHaveBeenCalledWith('s1', false)
  })

  it('reports a shift-click as shift=true', async () => {
    // A checkbox `change` event carries no modifier keys — only its `click`
    // does, and click always fires first. The row captures it there.
    const onToggle = vi.fn()
    render(<SelectableRow id="s1" name="A" checked={false} onToggle={onToggle}>x</SelectableRow>)
    const user = userEvent.setup()
    await user.keyboard('{Shift>}')
    await user.click(screen.getByRole('checkbox'))
    await user.keyboard('{/Shift}')
    expect(onToggle).toHaveBeenCalledWith('s1', true)
  })
})
