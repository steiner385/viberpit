// web/src/components/__tests__/GroupHeader.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GroupHeader } from '../GroupHeader'

function setup(over = {}) {
  const props = {
    groupKey: 'busy', label: 'Working', count: 2, collapsed: false,
    selected: true, listMode: true,
    onToggle: vi.fn(), onExpand: vi.fn(), onCollapse: vi.fn(),
    onNavigate: vi.fn(), onExit: vi.fn(), ...over,
  }
  render(<GroupHeader {...props} />)
  return props
}

describe('GroupHeader', () => {
  it('renders label, count and an expanded chevron; has a stable id', () => {
    setup()
    const btn = screen.getByRole('button', { name: /Working/ })
    expect(btn).toHaveAttribute('id', 'group-header-busy')
    expect(btn).toHaveAttribute('aria-expanded', 'true')
  })

  it('→ expands, ← collapses, Enter toggles (list mode)', async () => {
    const u = userEvent.setup()
    const p = setup({ collapsed: true })
    const btn = screen.getByRole('button', { name: /Working/ })
    btn.focus()
    await u.keyboard('{ArrowRight}')
    expect(p.onExpand).toHaveBeenCalledWith('busy')
    await u.keyboard('{ArrowLeft}')
    expect(p.onCollapse).toHaveBeenCalledWith('busy')
    await u.keyboard('{Enter}')
    expect(p.onToggle).toHaveBeenCalledWith('busy')
  })

  it('↑/↓ navigate and Esc exits', async () => {
    const u = userEvent.setup()
    const p = setup()
    screen.getByRole('button', { name: /Working/ }).focus()
    await u.keyboard('{ArrowDown}')
    expect(p.onNavigate).toHaveBeenCalledWith('down')
    await u.keyboard('{Escape}')
    expect(p.onExit).toHaveBeenCalled()
  })

  it('mouse click toggles', async () => {
    const u = userEvent.setup()
    const p = setup({ listMode: false, selected: false })
    await u.click(screen.getByRole('button', { name: /Working/ }))
    expect(p.onToggle).toHaveBeenCalledWith('busy')
  })
})
