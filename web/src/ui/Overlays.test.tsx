import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Dialog } from './Dialog'
import { Sheet } from './Sheet'

expect.extend(toHaveNoViolations)

describe('Dialog', () => {
  it('labels by title, traps focus, closes on Escape and backdrop, not when dismissable=false', async () => {
    const onClose = vi.fn()
    const { rerender } = render(
      <Dialog open onClose={onClose} title="Rename session"><button>Save</button></Dialog>,
    )
    const dlg = screen.getByRole('dialog', { name: 'Rename session' })
    expect(dlg).toHaveAttribute('aria-modal', 'true')
    expect(document.activeElement).toBe(screen.getByText('Save'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('dialog-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(2)
    expect(await axe(document.body)).toHaveNoViolations()

    rerender(<Dialog open onClose={onClose} title="Deploying" dismissable={false}><p>…</p></Dialog>)
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(screen.getByTestId('dialog-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(2)
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
})
