import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Tabs } from './Tabs'

// Scope note: axe in jsdom covers roles, names and ARIA wiring only. No
// stylesheet is applied here, so axe's `color-contrast` rule always returns
// `incomplete`, and `toHaveNoViolations()` inspects `violations` alone — these
// assertions therefore cannot fail on colour. Contrast is covered instead by
// src/__tests__/contrast.test.ts, which does the WCAG maths on index.css.
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

  it('Home key moves focus to the first tab', () => {
    const onChange = vi.fn()
    render(<Tabs items={items} value="a" onChange={onChange} label="Sections" />)
    const tabs = screen.getAllByRole('tab')
    tabs[2].focus()
    fireEvent.keyDown(tabs[2], { key: 'Home' })
    expect(document.activeElement).toBe(tabs[0])
  })

  it('ArrowLeft wraps from the first tab to the last', () => {
    const onChange = vi.fn()
    render(<Tabs items={items} value="a" onChange={onChange} label="Sections" />)
    const tabs = screen.getAllByRole('tab')
    tabs[0].focus()
    fireEvent.keyDown(tabs[0], { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(tabs[2])
  })

  it('ArrowRight wraps from the last tab to the first', () => {
    const onChange = vi.fn()
    render(<Tabs items={items} value="a" onChange={onChange} label="Sections" />)
    const tabs = screen.getAllByRole('tab')
    tabs[2].focus()
    fireEvent.keyDown(tabs[2], { key: 'ArrowRight' })
    expect(document.activeElement).toBe(tabs[0])
  })

  it('composes the combined count + attention accessible name', () => {
    const onChange = vi.fn()
    const combined = [{ id: 'a', label: 'Alpha', count: 3, attention: true }]
    render(<Tabs items={combined} value="a" onChange={onChange} label="Sections" />)
    expect(screen.getByRole('tab')).toHaveAccessibleName('Alpha (3, needs attention)')
  })

  it('vertical orientation follows Up/Down arrows and stacks tabs', async () => {
    const onChange = vi.fn()
    const { container } = render(
      <Tabs items={items} value="a" onChange={onChange} label="Sections" orientation="vertical" />,
    )
    const tablist = screen.getByRole('tablist', { name: 'Sections' })
    expect(tablist).toHaveAttribute('aria-orientation', 'vertical')
    const tabs = screen.getAllByRole('tab')
    tabs[0].focus()
    fireEvent.keyDown(tabs[0], { key: 'ArrowDown' })
    expect(document.activeElement).toBe(tabs[1])
    fireEvent.keyDown(tabs[1], { key: 'ArrowUp' })
    expect(document.activeElement).toBe(tabs[0])
    // Horizontal-only keys must not move focus in vertical orientation.
    fireEvent.keyDown(tabs[0], { key: 'ArrowRight' })
    expect(document.activeElement).toBe(tabs[0])
    expect(await axe(container)).toHaveNoViolations()
  })
})
