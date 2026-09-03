import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Dialog } from './Dialog'
import { Sheet } from './Sheet'

expect.extend(toHaveNoViolations)

describe('Dialog', () => {
  it('labels by title and moves initial focus into the panel', async () => {
    const onClose = vi.fn()
    render(
      <Dialog open onClose={onClose} title="Rename session"><button>Save</button></Dialog>,
    )
    const dlg = screen.getByRole('dialog', { name: 'Rename session' })
    expect(dlg).toHaveAttribute('aria-modal', 'true')
    expect(document.activeElement).toBe(screen.getByText('Save'))
    expect(await axe(document.body)).toHaveNoViolations()
  })

  it('labels by aria-label when there is no visible title', () => {
    render(
      <Dialog open onClose={() => {}} aria-label="Confirm delete"><button>Delete</button></Dialog>,
    )
    const dlg = screen.getByRole('dialog', { name: 'Confirm delete' })
    expect(dlg).toHaveAttribute('aria-label', 'Confirm delete')
    expect(dlg).not.toHaveAttribute('aria-labelledby')
  })

  it('closes on Escape when dismissable (default)', () => {
    const onClose = vi.fn()
    render(<Dialog open onClose={onClose} title="Rename session"><button>Save</button></Dialog>)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes on backdrop click when dismissable (default)', () => {
    const onClose = vi.fn()
    render(<Dialog open onClose={onClose} title="Rename session"><button>Save</button></Dialog>)
    fireEvent.click(screen.getByTestId('dialog-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('dismissable=false blocks Escape', () => {
    const onClose = vi.fn()
    render(<Dialog open onClose={onClose} title="Deploying" dismissable={false}><p>…</p></Dialog>)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('dismissable=false blocks backdrop click', () => {
    const onClose = vi.fn()
    render(<Dialog open onClose={onClose} title="Deploying" dismissable={false}><p>…</p></Dialog>)
    // Fire Escape first and assert unchanged, then the backdrop click and assert still
    // unchanged — each event is checked in isolation so a regression in either channel
    // points at exactly that channel, not a combined "something broke" failure.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('dialog-backdrop'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('renders into #overlays and inerts #root', () => {
    document.body.innerHTML = '<div id="root"></div><div id="overlays"></div>'
    render(<Dialog open onClose={() => {}} title="T"><button>x</button></Dialog>, { container: document.getElementById('root')! })
    expect(document.getElementById('overlays')!.querySelector('[role="dialog"]')).not.toBeNull()
    expect(document.getElementById('root')!.hasAttribute('inert')).toBe(true)
  })
})

describe('Sheet', () => {
  it('is a right-anchored dialog with a close button', () => {
    const onClose = vi.fn()
    render(<Sheet open onClose={onClose} title="Help"><p>body</p></Sheet>)
    expect(screen.getByRole('dialog', { name: 'Help' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close Help' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('overlay mode (default) portals to #overlays, inerts #root, shows a backdrop, and traps focus', () => {
    document.body.innerHTML = '<div id="root"></div><div id="overlays"></div>'
    render(
      <Sheet open onClose={() => {}} title="Overlay sheet"><button>focusable</button></Sheet>,
      { container: document.getElementById('root')! },
    )
    const overlaysRoot = document.getElementById('overlays')!
    const rootEl = document.getElementById('root')!
    // The panel lives in #overlays, not in #root and not loose in document.body.
    const dlgInOverlays = overlaysRoot.querySelector('[role="dialog"]')
    expect(dlgInOverlays).not.toBeNull()
    expect(rootEl.querySelector('[role="dialog"]')).toBeNull()
    expect(document.body.querySelector(':scope > [role="dialog"]')).toBeNull()
    // Modal: #root is inerted while the sheet is open.
    expect(rootEl.hasAttribute('inert')).toBe(true)
    // Backdrop is present (unlike push mode).
    expect(overlaysRoot.querySelector('[data-testid="sheet-backdrop"]')).not.toBeNull()
    // Focus trap is active: focus moved into the panel (its first focusable child is the
    // header's Close button, which comes before the body content in DOM order).
    expect(document.activeElement).not.toBe(document.body)
    expect(dlgInOverlays!.contains(document.activeElement)).toBe(true)
  })

  it('push mode renders inline with no portal, backdrop, or focus trap', () => {
    document.body.innerHTML = '<div id="root"></div><div id="overlays"></div>'
    const onClose = vi.fn()
    render(
      <Sheet open onClose={onClose} title="Inline panel" mode="push"><button>focusable</button></Sheet>,
      { container: document.getElementById('root')! },
    )
    // Rendered inline inside #root, not portaled to #overlays.
    expect(document.getElementById('overlays')!.querySelector('[role="dialog"]')).toBeNull()
    expect(document.getElementById('root')!.querySelector('[role="dialog"]')).not.toBeNull()
    // Not modal, no backdrop, and #root is not inerted (it is not modal).
    expect(screen.getByRole('dialog', { name: 'Inline panel' })).not.toHaveAttribute('aria-modal')
    expect(screen.queryByTestId('sheet-backdrop')).toBeNull()
    expect(document.getElementById('root')!.hasAttribute('inert')).toBe(false)
    // No focus trap: focus was not stolen into the panel.
    expect(document.activeElement).not.toBe(screen.getByText('focusable'))
  })

  it('side="bottom" renders as a bottom sheet, not a right-anchored one', () => {
    render(<Sheet open onClose={() => {}} title="Notice" side="bottom"><p>x</p></Sheet>)
    const dlg = screen.getByRole('dialog', { name: 'Notice' })
    // The `right` variant sets an inline width style; `bottom` never does.
    expect(dlg).not.toHaveAttribute('style')
    expect(dlg).toHaveClass('rounded-t-lg')
    expect(dlg).not.toHaveClass('border-l')
  })
})
