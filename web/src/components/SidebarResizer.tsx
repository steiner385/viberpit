import { useCallback, useRef } from 'react'
import { cn } from '@/lib/utils'
import { SIDEBAR_MIN, SIDEBAR_MAX } from '@/lib/sidebarWidth'

/** Keyboard nudge step, in px. */
const STEP = 16

interface SidebarResizerProps {
  width: number
  onResize: (px: number) => void
}

/**
 * Vertical drag handle that sits between the sidebar and the main pane. It is
 * the visual divider too (replacing the sidebar's right border). Pointer-drag
 * to resize; when focused, ← / → nudge by 16px and Home / End jump to the
 * min / max. Exposes ARIA separator semantics with valuenow/min/max so assistive
 * tech announces the current width.
 */
export function SidebarResizer({ width, onResize }: SidebarResizerProps) {
  const draggingRef = useRef(false)

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault()
    draggingRef.current = true
    const startX = e.clientX
    const startW = width
    function onMove(ev: PointerEvent) {
      if (!draggingRef.current) return
      onResize(startW + (ev.clientX - startX))
    }
    function onUp() {
      draggingRef.current = false
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }, [width, onResize])

  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'ArrowLeft') { e.preventDefault(); onResize(width - STEP) }
    else if (e.key === 'ArrowRight') { e.preventDefault(); onResize(width + STEP) }
    else if (e.key === 'Home') { e.preventDefault(); onResize(SIDEBAR_MIN) }
    else if (e.key === 'End') { e.preventDefault(); onResize(SIDEBAR_MAX) }
  }, [width, onResize])

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sidebar"
      aria-valuenow={width}
      aria-valuemin={SIDEBAR_MIN}
      aria-valuemax={SIDEBAR_MAX}
      tabIndex={0}
      title="Drag to resize · ← → to nudge"
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
      className={cn(
        'group relative shrink-0 w-1.5 cursor-col-resize self-stretch select-none touch-none',
        'bg-gray-200 dark:bg-gray-700 hover:bg-blue-400 dark:hover:bg-blue-500',
        'focus-visible:outline-none focus-visible:bg-blue-500',
        'transition-colors',
      )}
    >
      {/* Center grip, and a wider invisible hit area for easier grabbing. */}
      <span
        aria-hidden="true"
        className="absolute inset-y-0 -left-1.5 -right-1.5"
      />
      <span
        aria-hidden="true"
        className={cn(
          'absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2',
          'h-10 w-0.5 rounded-full bg-gray-400 dark:bg-gray-500',
          'group-hover:bg-white group-focus-visible:bg-white',
        )}
      />
    </div>
  )
}
