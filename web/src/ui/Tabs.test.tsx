import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Tabs } from './Tabs'

expect.extend(toHaveNoViolations)
const items = [{ id: 'a', label: 'Alpha', count: 3 }, { id: 'b', label: 'Beta', attention: true }, { id: 'c', label: 'Gamma' }]

describe('Tabs', () => {
  it('roving tabindex + arrow keys move focus; click selects', async () => {
    const onChange = vi.fn()
    const { container } = render(<Tabs items={items} value="a" onChange={onChange} label="Sections" />)
    const tabs = screen.getAllByRole('tab')
    expect(tabs[0]).toHaveAttribute('tabindex', '0')
    expect(tabs[1]).toHaveAttribute('tabindex', '-1')
    expect(tabs[0]).toHaveAccessibleName('Alpha (3)')
    expect(tabs[1]).toHaveAccessibleName('Beta (needs attention)')
    tabs[0].focus()
    fireEvent.keyDown(tabs[0], { key: 'ArrowRight' })
    expect(document.activeElement).toBe(tabs[1])
    fireEvent.keyDown(tabs[1], { key: 'End' })
    expect(document.activeElement).toBe(tabs[2])
    fireEvent.click(tabs[2])
    expect(onChange).toHaveBeenCalledWith('c')
    expect(await axe(container)).toHaveNoViolations()
  })
})
