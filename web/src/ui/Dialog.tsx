import * as React from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
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
  useFocusTrap(panelRef, open, { onClose: dismissable ? onClose : noop })

  React.useEffect(() => {
    if (open && initialFocus?.current) initialFocus.current.focus()
  }, [open, initialFocus])

  if (!open) return null

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
