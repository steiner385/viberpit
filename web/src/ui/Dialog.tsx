import * as React from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
import { useInteractive } from '@/shell/InteractionContext'
import { getOverlayRoot } from './portal'
import { useFocusTrap } from './useFocusTrap'

const SIZES = { sm: 'max-w-sm', md: 'max-w-md', lg: 'max-w-lg', xl: 'max-w-5xl' } as const

/** A dialog must be named by exactly one of a visible `title` or an `aria-label` — never
 *  neither (silently unlabelled) and never both (redundant/conflicting accessible name). */
type DialogLabelling = { title: string; 'aria-label'?: never } | { title?: never; 'aria-label': string }

export type DialogProps = DialogLabelling & {
  open: boolean
  onClose: () => void
  description?: React.ReactNode
  size?: keyof typeof SIZES
  initialFocus?: React.RefObject<HTMLElement | null>
  /** false → neither Escape nor the backdrop closes it (e.g. a running deploy). */
  dismissable?: boolean
  className?: string
  bodyClassName?: string
  children: React.ReactNode
}

export function Dialog({
  open,
  onClose,
  title,
  'aria-label': ariaLabel,
  description,
  size = 'md',
  initialFocus,
  dismissable = true,
  className,
  bodyClassName,
  children,
}: DialogProps) {
  const panelRef = React.useRef<HTMLDivElement>(null)
  const titleId = React.useId()
  const descId = React.useId()
  const noop = React.useCallback(() => {}, [])

  // Kiosk (`InteractionProvider interactive={false}`) refuses modals outright.
  // The refusal lives in the PRIMITIVE and not in `AppShell` because a dialog
  // can be opened from anywhere — a page, a drawer, a websocket event
  // (`ModelSwitchDialog`, `CommandResultOverlay`) — and this component is the
  // only place that sees all of them. A wall display cannot dismiss a modal, so
  // one that slipped through would cover the display until someone walked over
  // with a keyboard.
  const interactive = useInteractive()
  const shown = open && interactive

  // `console.warn` from an EFFECT, not from render: render runs twice under
  // StrictMode and again on every parent re-render, so warning inline would
  // print the same refusal indefinitely. The flag resets when the dialog closes,
  // so each attempt to open one is reported exactly once.
  const warned = React.useRef(false)
  React.useEffect(() => {
    if (open && !interactive) {
      if (warned.current) return
      warned.current = true
      console.warn(`Dialog refused in read-only (kiosk) mode: ${title ?? ariaLabel}`)
    } else {
      warned.current = false
    }
  }, [open, interactive, title, ariaLabel])

  // `shown`, not `open`: a refused dialog must not trap focus or `inert` #root
  // either — it is not on screen to trap focus into.
  useFocusTrap(panelRef, shown, { onClose: dismissable ? onClose : noop })

  React.useEffect(() => {
    if (shown && initialFocus?.current) initialFocus.current.focus()
  }, [shown, initialFocus])

  if (!shown) return null

  const handleBackdropClick = dismissable ? onClose : undefined

  return createPortal(
    <div className="fixed inset-0 z-dialog flex items-end sm:items-center justify-center p-4">
      <div
        data-testid="dialog-backdrop"
        className="absolute inset-0 bg-black/50"
        onClick={handleBackdropClick}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-label={title ? undefined : ariaLabel}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={cn(
          'relative w-full rounded-lg border border-edge bg-surface text-ink shadow-xl p-6 space-y-4 max-h-[90vh] overflow-y-auto',
          SIZES[size],
          className,
        )}
      >
        {title && (
          <h2 id={titleId} className="text-lg font-semibold">
            {title}
          </h2>
        )}
        {description && (
          <p id={descId} className="text-sm text-ink-muted">
            {description}
          </p>
        )}
        <div className={bodyClassName}>{children}</div>
      </div>
    </div>,
    getOverlayRoot(),
  )
}
