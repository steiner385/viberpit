import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ViewModeMenu } from './ViewModeMenu'

describe('ViewModeMenu', () => {
  it('labels the trigger "View: <Mode>"', () => {
    render(<ViewModeMenu mode="thinking" onSelect={() => {}} />)
    expect(screen.getByRole('button', { name: 'View: Thinking' })).toHaveTextContent('View: Thinking')
  })

  it('keeps the accessible name "View: <Mode>" while the visible prefix is md-up only, on one line', () => {
    render(<ViewModeMenu mode="normal" onSelect={() => {}} />)
    const trigger = screen.getByRole('button', { name: 'View: Normal' })
    expect(trigger).toHaveAccessibleName('View: Normal')
    expect(trigger).toHaveClass('whitespace-nowrap')
    const prefix = within(trigger).getByText('View:', { exact: false, selector: 'span.hidden' })
    expect(prefix).toHaveClass('hidden', 'md:inline')
  })

  it('lists the three modes in order and checks the current one', async () => {
    render(<ViewModeMenu mode="verbose" onSelect={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: /^View:/ }))
    const menu = screen.getByRole('menu', { name: 'Transcript view' })
    const items = within(menu).getAllByRole('menuitemradio')
    expect(items.map((el) => el.textContent?.replace(/\d+$/, ''))).toEqual(['Normal', 'Thinking', 'Verbose'])
    expect(within(menu).getByRole('menuitemradio', { name: /^Verbose/ })).toHaveAttribute('aria-checked', 'true')
    expect(within(menu).getByRole('menuitemradio', { name: /^Normal/ })).toHaveAttribute('aria-checked', 'false')
  })

  it('calls onSelect with the picked mode and closes', async () => {
    const onSelect = vi.fn()
    render(<ViewModeMenu mode="normal" onSelect={onSelect} />)
    await userEvent.click(screen.getByRole('button', { name: /^View:/ }))
    await userEvent.click(screen.getByRole('menuitemradio', { name: /^Thinking/ }))
    expect(onSelect).toHaveBeenCalledWith('thinking')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})
