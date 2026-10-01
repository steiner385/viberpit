import * as React from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { getOverlayRoot } from './portal'
import { useFocusTrap } from './useFocusTrap'
import { IconButton } from './IconButton'

/**
 * Everything else lands on the panel element (the `Button`/`Card` convention).
 * `AppShell` needs this: an overlay sheet portals out of the layout tree, so its
 * `data-region` marker has to ride on the panel itself — a marker left behind in
 * the layout would claim the region is present while the panel it names is
 * somewhere else entirely.
 */
type PanelAttributes = Omit<React.HTMLAttributes<HTMLDivElement>, 'title' | 'onClose' | 'children'>

export interface SheetProps extends PanelAttributes {
  open: boolean
  onClose: () => void
  title: string
  side?: 'right' | 'bottom'
  /** `push` renders inline (no portal, no backdrop, no focus trap) — the shell
   *  lays it out as a column beside the content; it is not modal. It still
   *  honours a non-modal dialog's keyboard contract: focus moves to its Close
   *  button on open, Escape closes it while focus is inside, and focus returns
   *  to where it was on close. */
  mode?: 'overlay' | 'push'
  width?: number | string
  initialFocus?: React.RefObject<HTMLElement | null>
  /** Element to return focus to on close (e.g. the button that opened the sheet). */
  returnFocusRef?: React.RefObject<HTMLElement | null>
  className?: string
  children: React.ReactNode
}

export function Sheet({
  open,
  onClose,
  title,
  side = 'right',
  mode = 'overlay',
  width = 448,
  initialFocus,
  returnFocusRef,
  className,
  children,
  ...panelProps
}: SheetProps) {
  const panelRef = React.useRef<HTMLDivElement>(null)
  const closeRef = React.useRef<HTMLButtonElement>(null)
  const titleId = React.useId()
  const trapActive = open && mode === 'overlay'
  useFocusTrap(panelRef, trapActive, { onClose, returnFocusRef })

  React.useEffect(() => {
    if (open && mode === 'overlay' && initialFocus?.current) initialFocus.current.focus()
  }, [open, mode, initialFocus])

  // Push mode has no trap (it is not modal), but it is still a dialog that a
  // key opened: focus has to land in it or the keyboard user is left where
  // they were, with a column they cannot reach — measured at ≥1280px: Ctrl+/
  // opened the help drawer with focus on `body`, Esc did nothing, and the Close
  // button was not reached in 40 Tab presses, while the same key at 1100px
  // (overlay) landed on Close and Esc closed it (2026-10-01 shell critique, P1).
  // Same landing as the overlay's trap (its first focusable is Close), and the
  // same return on close, so the behaviour no longer flips with window width.
  // Both refs are read through a layout-synced ref, the way `useFocusTrap`
  // reads its `opts`: the cleanup wants the CURRENT return target, not the one
  // captured when the panel opened.
  const latest = React.useRef({ onClose, returnFocusRef })
  React.useLayoutEffect(() => { latest.current = { onClose, returnFocusRef } })
  React.useEffect(() => {
    if (!open || mode !== 'push') return
    const previous = document.activeElement as HTMLElement | null
    const target = initialFocus?.current ?? closeRef.current
    target?.focus()
    return () => { (latest.current.returnFocusRef?.current ?? previous)?.focus() }
  }, [open, mode, initialFocus])

  // Escape closes a non-modal dialog only while focus is inside it: a column
  // beside `<main>` must not swallow the Escape a focused session owns
  // (`insession.detach`). `preventDefault` is what the window-level shortcut
  // listener honours, so the key stops here.
  const onPanelKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (mode !== 'push' || e.key !== 'Escape' || e.defaultPrevented) return
    e.preventDefault()
    latest.current.onClose()
  }

  if (!open) return null

  const panel = (
    <div
      {...panelProps}
      ref={panelRef}
      role="dialog"
      aria-modal={mode === 'overlay' ? 'true' : undefined}
      aria-labelledby={titleId}
      tabIndex={-1}
      onKeyDown={onPanelKeyDown}
      style={side === 'right' ? { width } : undefined}
      className={cn(
        'flex flex-col bg-surface text-ink border-edge shadow-xl overflow-hidden',
        side === 'right'
          ? 'h-full max-w-full border-l animate-sheet-right motion-reduce:animate-none'
          : 'w-full max-h-[85vh] rounded-t-lg border-t animate-sheet-bottom motion-reduce:animate-none',
        mode === 'overlay' && 'relative',
        className,
      )}
    >
      {/* A `<div>`, not a `<header>`: `<header>` maps to the BANNER landmark
          unless it is inside article/aside/main/nav/section, and a
          `div[role="dialog"]` is none of those. A sheet open beside the phone
          `TopBar` therefore gave the document two banner landmarks and two
          unnamed landmarks of the same role (axe `landmark-no-duplicate-banner`
          + `landmark-unique`, caught by `AppShell.test.tsx`). The panel is
          already named by `aria-labelledby` on the dialog, so nothing is lost. */}
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-edge shrink-0">
        <h2 id={titleId} className="text-base font-semibold">
          {title}
        </h2>
        <IconButton ref={closeRef} aria-label={`Close ${title}`} size="sm" onClick={onClose}>
          <X size={16} aria-hidden="true" />
        </IconButton>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-4">{children}</div>
    </div>
  )

  if (mode === 'push') return panel

  return createPortal(
    <div className={cn('fixed inset-0 z-drawer flex', side === 'right' ? 'justify-end' : 'items-end')}>
      <div data-testid="sheet-backdrop" className="absolute inset-0 bg-black/50" onClick={onClose} aria-hidden="true" />
      {panel}
    </div>,
    getOverlayRoot(),
  )
}
