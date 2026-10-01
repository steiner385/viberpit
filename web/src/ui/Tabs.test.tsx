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

  // 2026-10-01 Delivery critique, P0: an `overflow-x-auto` strip is a scroll
  // container with a zero automatic min-height, so inside a `min-h-0` flex
  // column a tall panel squeezed it to its 1px border on 9 of 11 detail routes.
  // The strip refuses to shrink itself; no caller has to remember to.
  it('never yields its height to the panel below it (shrink-0 on both orientations)', () => {
    const { rerender } = render(<Tabs items={items} value="a" onChange={vi.fn()} label="Sections" />)
    expect(screen.getByRole('tablist')).toHaveClass('shrink-0')
    rerender(<Tabs items={items} value="a" onChange={vi.fn()} label="Sections" orientation="vertical" />)
    expect(screen.getByRole('tablist')).toHaveClass('shrink-0')
  })

  it('composes the combined count + attention accessible name', () => {
    const onChange = vi.fn()
    const combined = [{ id: 'a', label: 'Alpha', count: 3, attention: true }]
    render(<Tabs items={combined} value="a" onChange={onChange} label="Sections" />)
    expect(screen.getByRole('tab')).toHaveAccessibleName('Alpha (3, needs attention)')
  })

  it('a strip wider than its box scrolls the selected tab into view and fades the edge hiding tabs', () => {
    const onChange = vi.fn()
    const { rerender } = render(<Tabs items={items} value="a" onChange={onChange} label="Sections" />)
    const list = screen.getByRole('tablist')
    const tabs = screen.getAllByRole('tab')
    // jsdom has no layout: give the strip a 100px box holding 300px of tabs.
    Object.defineProperty(list, 'clientWidth', { value: 100, configurable: true })
    Object.defineProperty(list, 'scrollWidth', { value: 300, configurable: true })
    let scrollLeft = 0
    Object.defineProperty(list, 'scrollLeft', { get: () => scrollLeft, set: (v: number) => { scrollLeft = v }, configurable: true })
    tabs.forEach((t, i) => {
      Object.defineProperty(t, 'offsetLeft', { value: i * 100, configurable: true })
      Object.defineProperty(t, 'offsetWidth', { value: 100, configurable: true })
    })
    // Nothing scrolled yet, so only the right edge hides tabs.
    fireEvent.scroll(list)
    expect(list).not.toHaveAttribute('data-hidden-left')
    expect(list).toHaveAttribute('data-hidden-right', 'true')
    // Selecting Gamma (200–300px) scrolls it, plus its 24px gutter, into the box.
    rerender(<Tabs items={items} value="c" onChange={onChange} label="Sections" />)
    expect(scrollLeft).toBe(224)
    expect(list).toHaveAttribute('data-hidden-left', 'true')
    expect(list).not.toHaveAttribute('data-hidden-right')
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
