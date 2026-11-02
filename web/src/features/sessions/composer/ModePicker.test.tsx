import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ModePicker } from './ModePicker'
import { SESSION_MODE_OPTIONS } from '@/session/permissionModes'

function renderPicker(over: Partial<React.ComponentProps<typeof ModePicker>> = {}) {
  const onSelect = vi.fn()
  render(<ModePicker mode="interactive" options={SESSION_MODE_OPTIONS} onSelect={onSelect} {...over} />)
  return { onSelect }
}

describe('ModePicker', () => {
  it('shows the current mode label (Plan for plan)', () => {
    renderPicker({ mode: 'plan' })
    expect(screen.getByRole('button', { name: /mode/i })).toHaveTextContent('Plan')
  })

  it("reads the SDK 'default' sentinel as Ask", () => {
    renderPicker({ mode: 'default' })
    expect(screen.getByRole('button', { name: /mode/i })).toHaveTextContent('Ask')
  })

  it('lists the Desktop group then the labelled ViberPit group, with the exact labels', async () => {
    renderPicker()
    await userEvent.click(screen.getByRole('button', { name: /mode/i }))
    const menu = screen.getByRole('menu', { name: 'Permission mode' })
    const groups = within(menu).getAllByRole('group')
    expect(groups).toHaveLength(2)
    const names = (g: HTMLElement) => within(g).getAllByRole('menuitemradio').map((el) => el.textContent?.replace(/\d+$/, ''))
    expect(names(groups[0])).toEqual(['Ask', 'Accept edits', 'Plan', 'Auto', 'Bypass'])
    expect(groups[1]).toHaveAccessibleName('ViberPit')
    expect(within(groups[1]).getByText('ViberPit')).toBeInTheDocument()
    expect(names(groups[1])).toEqual(['Read-only allowlist', 'Full auto'])
  })

  it('marks the current mode checked', async () => {
    renderPicker({ mode: 'default' })
    await userEvent.click(screen.getByRole('button', { name: /mode/i }))
    expect(screen.getByRole('menuitemradio', { name: /^Ask/ })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('menuitemradio', { name: /^Plan/ })).toHaveAttribute('aria-checked', 'false')
  })

  it('choosing Full auto calls onSelect("full-auto") and closes the menu', async () => {
    const { onSelect } = renderPicker()
    await userEvent.click(screen.getByRole('button', { name: /mode/i }))
    await userEvent.click(screen.getByRole('menuitemradio', { name: /^Full auto/ }))
    expect(onSelect).toHaveBeenCalledWith('full-auto')
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('shows "Mode change pending" as a status while a change is pending', () => {
    renderPicker({ mode: 'auto', pendingMode: 'plan' })
    expect(screen.getByRole('status')).toHaveTextContent('Mode change pending')
  })

  it('shows no pending status otherwise', () => {
    renderPicker({ mode: 'auto' })
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('can be opened by its controlled open prop', () => {
    const onOpenChange = vi.fn()
    renderPicker({ open: true, onOpenChange })
    expect(screen.getByRole('menu', { name: 'Permission mode' })).toBeInTheDocument()
  })
})
