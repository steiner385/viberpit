/**
 * The session composer's `+` menu (D-14): Attach and the slash-command palette — the
 * two things that used to be a lone 📎 button and a "type /" secret. Presets apply at
 * creation, so they are the dispatch bar's, never offered here.
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi } from 'vitest'
import type { Command } from '@/session/events'
import { Composer } from './Composer'

vi.mock('@/api/hooks', () => ({ useSessions: () => ({ data: [] }) }))

const CMD: Command = { name: 'compact', description: 'Compact the conversation', argumentHint: null, kind: 'passthrough', source: 'vendor' }

function mount(commands: Command[] = [CMD]) {
  return render(<Composer status={{ state: 'idle', count: 0 }} onPrompt={() => {}} onInterrupt={() => {}} commands={commands} />)
}

describe('Composer + menu', () => {
  it('replaces the standalone attach button', () => {
    mount()
    expect(screen.queryByRole('button', { name: 'Attach image' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Add' })).toHaveAttribute('aria-haspopup', 'menu')
  })

  it('lists Attach and Slash commands, and no presets', async () => {
    mount()
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    const items = screen.getAllByRole('menuitem').map((i) => i.textContent?.replace(/\d$/, ''))
    expect(items).toEqual(['Attach file', 'Slash commands'])
  })

  it('Attach opens the file picker', async () => {
    const { container } = mount()
    const input = container.querySelector('input[type=file]') as HTMLInputElement
    const click = vi.spyOn(input, 'click')
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await userEvent.click(screen.getByRole('menuitem', { name: /Attach file/ }))
    expect(click).toHaveBeenCalledTimes(1)
  })

  it('Slash commands opens the palette in the message box', async () => {
    mount()
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await userEvent.click(screen.getByRole('menuitem', { name: /Slash commands/ }))
    expect(screen.getByLabelText('Message')).toHaveValue('/')
    expect(screen.getByLabelText('Message')).toHaveFocus()
    expect(screen.getByRole('listbox')).toHaveTextContent('compact')
  })

  it('offers no Slash commands entry when the session has none', async () => {
    mount([])
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(screen.queryByRole('menuitem', { name: /Slash commands/ })).toBeNull()
  })
})
