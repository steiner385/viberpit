import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { InteractionProvider } from '@/shell/InteractionContext'
import { Dialog } from './Dialog'
import { Sheet } from './Sheet'

// Scope note: axe in jsdom covers roles, names and ARIA wiring only. No
// stylesheet is applied here, so axe's `color-contrast` rule always returns
// `incomplete`, and `toHaveNoViolations()` inspects `violations` alone — these
// assertions therefore cannot fail on colour. Contrast is covered instead by
// src/__tests__/contrast.test.ts, which does the WCAG maths on index.css.
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

/**
 * Kiosk refuses modals (Task 19).
 *
 * The default OUTSIDE a provider is `interactive: true` on purpose
 * (`shell/InteractionContext.tsx`), which is why every case above — and every
 * other dialog test in the suite — is unaffected by this. The first case here
 * pins that default explicitly, so a future flip of it reddens this file rather
 * than silently turning the whole suite read-only.
 */
describe('Dialog — read-only (kiosk) refusal', () => {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
  afterEach(() => warn.mockClear())

  it('still opens under interactive={true}, and outside any provider', () => {
    const outside = render(<Dialog open onClose={() => {}} title="Rename session"><button>Save</button></Dialog>)
    expect(screen.getByRole('dialog', { name: 'Rename session' })).toBeInTheDocument()
    outside.unmount()

    render(
      <InteractionProvider interactive={true}>
        <Dialog open onClose={() => {}} title="Rename session"><button>Save</button></Dialog>
      </InteractionProvider>,
    )
    expect(screen.getByRole('dialog', { name: 'Rename session' })).toBeInTheDocument()
    expect(warn).not.toHaveBeenCalled()
  })

  it('renders no dialog node under interactive={false} and warns once with the title', () => {
    const { rerender } = render(
      <InteractionProvider interactive={false}>
        <Dialog open onClose={() => {}} title="Rename session"><button>Save</button></Dialog>
      </InteractionProvider>,
    )
    expect(screen.queryByRole('dialog')).toBeNull()
    // Not merely unlabelled or hidden — the panel's own content is absent too.
    expect(screen.queryByText('Save')).toBeNull()
    expect(screen.queryByTestId('dialog-backdrop')).toBeNull()
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0][0]).toContain('Rename session')

    // A re-render of a still-open refused dialog does not re-warn.
    rerender(
      <InteractionProvider interactive={false}>
        <Dialog open onClose={() => {}} title="Rename session"><button>Save</button></Dialog>
      </InteractionProvider>,
    )
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('does not trap focus or inert #root when refused', () => {
    document.body.innerHTML = '<div id="root"></div><div id="overlays"></div>'
    render(
      <InteractionProvider interactive={false}>
        <Dialog open onClose={() => {}} aria-label="Confirm delete"><button>Delete</button></Dialog>
      </InteractionProvider>,
      { container: document.getElementById('root')! },
    )
    // The refusal must not leave the page behind it disabled — `inert` on
    // `#root` with nothing on top is an app nobody can use.
    expect(document.getElementById('root')!.hasAttribute('inert')).toBe(false)
    expect(document.getElementById('overlays')!.querySelector('[role="dialog"]')).toBeNull()
    expect(warn.mock.calls[0][0]).toContain('Confirm delete')
  })

  it('does not close-then-warn: a closed dialog under interactive={false} is silent', () => {
    render(
      <InteractionProvider interactive={false}>
        <Dialog open={false} onClose={() => {}} title="Rename session"><button>Save</button></Dialog>
      </InteractionProvider>,
    )
    expect(warn).not.toHaveBeenCalled()
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
