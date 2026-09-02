import * as React from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { getOverlayRoot } from './portal'
import { useFocusTrap } from './useFocusTrap'
import { IconButton } from './IconButton'

export interface SheetProps {
  open: boolean
  onClose: () => void
  title: string
  side?: 'right' | 'bottom'
  /** `push` renders inline (no portal, no backdrop, no focus trap) — the shell
   *  lays it out as a column beside the content; it is not modal. */
  mode?: 'overlay' | 'push'
  width?: number | string
  initialFocus?: React.RefObject<HTMLElement | null>
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
  className,
  children,
}: SheetProps) {
  const panelRef = React.useRef<HTMLDivElement>(null)
  const titleId = React.useId()
  const trapActive = open && mode === 'overlay'
  useFocusTrap(panelRef, trapActive, { onClose })

  React.useEffect(() => {
    if (open && mode === 'overlay' && initialFocus?.current) initialFocus.current.focus()
  }, [open, mode, initialFocus])

  if (!open) return null

  const panel = (
    <div
      ref={panelRef}
      role="dialog"
      aria-modal={mode === 'overlay' ? 'true' : undefined}
      aria-labelledby={titleId}
      tabIndex={-1}
      style={side === 'right' ? { width } : undefined}
      className={cn(
        'flex flex-col bg-surface text-ink border-edge shadow-xl overflow-hidden',
        side === 'right' ? 'h-full max-w-full border-l' : 'w-full max-h-[85vh] rounded-t-lg border-t',
        mode === 'overlay' && 'relative',
        className,
      )}
    >
      <header className="flex items-center justify-between gap-3 px-4 py-3 border-b border-edge shrink-0">
        <h2 id={titleId} className="text-base font-semibold">
          {title}
        </h2>
        <IconButton aria-label={`Close ${title}`} size="sm" onClick={onClose}>
          <X size={16} aria-hidden="true" />
        </IconButton>
      </header>
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
