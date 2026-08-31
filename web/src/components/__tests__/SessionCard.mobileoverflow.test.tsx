/**
 * Unified `…` menu — the single overflow that surfaces Quick Replies plus
 * Rename, Archive, Delete (and Autopilot when applicable) on EVERY device.
 * There is no desktop/mobile split: one trigger, one menu, all breakpoints.
 */
import { render, screen, fireEvent, within } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const noop = () => {}

function mkSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 's1',
    name: 'Fix login bug',
    accountName: 'work',
    status: 'done',
    createdAt: '2026-06-22T11:00:00Z',
    updatedAt: '2026-06-22T11:58:00Z',
    ...overrides,
  }
}

function renderCard(
  overrides: Partial<Session> = {},
  handlers: {
    onRename?: (id: string) => void
    onClose?: (id: string) => void
    onDelete?: (id: string) => void
    onToggleAutopilot?: (id: string, paused: boolean) => void
  } = {},
) {
  return render(
    <SessionCard
      session={mkSession(overrides)}
      account={undefined}
      onOpen={noop}
      onRename={handlers.onRename ?? noop}
      onClose={handlers.onClose ?? noop}
      onDelete={handlers.onDelete ?? noop}
      onQuickResume={noop}
      onToggleAutopilot={handlers.onToggleAutopilot}
    />,
  )
}

describe('SessionCard mobile overflow menu', () => {
  it('renders a `…` overflow trigger button', () => {
    renderCard()
    expect(screen.getByRole('button', { name: /more actions for session/i })).toBeInTheDocument()
  })

  it('overflow trigger has aria-haspopup="menu" and aria-expanded=false when closed', () => {
    renderCard()
    const trigger = screen.getByRole('button', { name: /more actions for session/i })
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  it('overflow menu is not in the DOM when closed', () => {
    renderCard()
    // The dropdown mounts only when open — not a visibility trick.
    expect(screen.queryByRole('menu', { name: /session actions/i })).not.toBeInTheDocument()
  })

  it('opens the overflow menu on trigger click', () => {
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
    const menu = screen.queryByRole('menu')
    expect(menu).toBeInTheDocument()
  })

  it('aria-expanded reflects open state', () => {
    renderCard()
    const trigger = screen.getByRole('button', { name: /more actions for session/i })
    fireEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
  })

  it('overflow menu contains Rename, Archive, and Delete', () => {
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
    // Items are role="menuitem" inside the menu.
    expect(screen.getByRole('menuitem', { name: /rename/i })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /archive/i })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /delete/i })).toBeInTheDocument()
  })

  it('Rename menuitem fires onRename and closes the menu', () => {
    const onRename = vi.fn()
    renderCard({}, { onRename })
    fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /rename/i }))
    expect(onRename).toHaveBeenCalledWith('s1')
    // Menu unmounts after the action.
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('Archive menuitem fires onClose and closes the menu', () => {
    const onClose = vi.fn()
    renderCard({}, { onClose })
    fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /archive/i }))
    expect(onClose).toHaveBeenCalledWith('s1')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('Delete menuitem fires onDelete and closes the menu', () => {
    const onDelete = vi.fn()
    renderCard({}, { onDelete })
    fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /delete/i }))
    expect(onDelete).toHaveBeenCalledWith('s1')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('closes the overflow menu on Escape key', () => {
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
    expect(screen.getByRole('menu')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('closes the overflow menu on outside click', () => {
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
    expect(screen.getByRole('menu')).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  describe('Autopilot in overflow', () => {
    it('shows Autopilot menuitem when onToggleAutopilot is provided', () => {
      renderCard({ autopilotPaused: false }, { onToggleAutopilot: noop })
      fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
      expect(screen.getByTestId('card-overflow-autopilot')).toBeInTheDocument()
    })

    it('omits Autopilot menuitem when onToggleAutopilot is not provided', () => {
      renderCard({})
      fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
      expect(screen.queryByTestId('card-overflow-autopilot')).not.toBeInTheDocument()
    })

    it('labels autopilot menuitem "Pause autopilot" when autopilot is on', () => {
      renderCard({ autopilotPaused: false }, { onToggleAutopilot: noop })
      fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
      const item = within(screen.getByRole('menu')).getByTestId('card-overflow-autopilot')
      expect(item).toHaveTextContent('Pause autopilot')
    })

    it('labels autopilot menuitem "Resume autopilot" when autopilot is paused', () => {
      renderCard({ autopilotPaused: true }, { onToggleAutopilot: noop })
      fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
      const item = within(screen.getByRole('menu')).getByTestId('card-overflow-autopilot')
      expect(item).toHaveTextContent('Resume autopilot')
    })

    it('fires onToggleAutopilot with the opposite state and closes the menu', () => {
      const onToggleAutopilot = vi.fn()
      renderCard({ autopilotPaused: false }, { onToggleAutopilot })
      fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
      fireEvent.click(screen.getByTestId('card-overflow-autopilot'))
      expect(onToggleAutopilot).toHaveBeenCalledWith('s1', true)
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    })
  })

  describe('Quick Replies inside the menu', () => {
    it('surfaces the canned quick replies at the top of the menu for a resumable session', () => {
      renderCard({ status: 'done' })
      fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
      expect(screen.getByRole('menuitem', { name: 'continue' })).toBeInTheDocument()
    })

    it('there is no separate quick-replies trigger button', () => {
      renderCard({ status: 'done' })
      expect(screen.queryByRole('button', { name: /quick replies/i })).not.toBeInTheDocument()
    })
  })

  describe('one menu for all breakpoints', () => {
    it('the overflow trigger is not gated behind a viewport breakpoint', () => {
      renderCard()
      const trigger = screen.getByRole('button', { name: /more actions for session/i })
      // The unified menu replaces the old lg:hidden mobile-only overflow and the
      // hidden lg:contents desktop icon row — nothing about it is viewport-gated.
      expect(trigger.parentElement).not.toHaveClass('lg:hidden')
      expect(screen.queryByTestId('card-secondary-actions')).not.toBeInTheDocument()
    })
  })
})
