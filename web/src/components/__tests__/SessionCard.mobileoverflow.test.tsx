/**
 * Mobile overflow menu — the `…` button that surfaces Rename, Archive, Delete
 * (and Autopilot when applicable) on viewports below lg (1024px).
 *
 * In jsdom `lg:` viewport breakpoints never fire, so `lg:hidden` elements are
 * always rendered and `hidden lg:contents` elements are always hidden. Tests
 * therefore exercise the mobile path by default — the overflow button is always
 * present and the desktop secondary group is always hidden in jsdom.
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

  describe('Quick Replies always visible', () => {
    it('Quick Replies button is present regardless of overflow state', () => {
      renderCard({ status: 'done' })
      // Always-visible button outside the secondary group.
      expect(screen.getByRole('button', { name: /quick replies/i })).toBeInTheDocument()
    })

    it('Quick Replies is NOT inside the desktop secondary-actions group', () => {
      renderCard({ status: 'done' })
      const secondary = screen.getByTestId('card-secondary-actions')
      const quickReplies = screen.getByRole('button', { name: /quick replies/i })
      expect(secondary).not.toContainElement(quickReplies)
    })
  })

  describe('overflow button visibility class', () => {
    it('overflow wrapper has lg:hidden so it disappears at the desktop breakpoint', () => {
      renderCard()
      const trigger = screen.getByRole('button', { name: /more actions for session/i })
      // The overflow button lives inside a div.relative.lg:hidden wrapper.
      expect(trigger.parentElement).toHaveClass('lg:hidden')
    })
  })
})
