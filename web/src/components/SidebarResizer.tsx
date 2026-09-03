import { useCallback, useRef } from 'react'
import { cn } from '@/lib/utils'
import { SIDEBAR_MIN, SIDEBAR_MAX } from '@/lib/sidebarWidth'

/** Keyboard nudge step, in px. */
const STEP = 16

interface SidebarResizerProps {
  width: number
  onResize: (px: number) => void
  /** Which side of the handle the resized panel is on. A left-hand panel (the
   *  sidebar) grows as the handle moves right; a right-hand one (`app.drawer`)
   *  grows as it moves left. Getting this wrong makes the panel run away from
   *  the pointer, so it is a parameter rather than an assumption. */
  panelSide?: 'left' | 'right'
  min?: number
  max?: number
  /** Accessible name — there can be two handles on screen at once (list column
   *  and drawer), and "Resize sidebar" twice names neither. */
  label?: string
}

/**
 * Vertical drag handle between a resizable panel and the pane beside it. It is
 * the visual divider too (replacing that panel's border). Pointer-drag to
 * resize; when focused, ← / → nudge by 16px and Home / End jump to the
 * min / max. Exposes ARIA separator semantics with valuenow/min/max so assistive
 * tech announces the current width.
 *
 * The defaults are the sidebar's, which is what every existing caller wants;
 * `app.drawer` passes its own band, label and `panelSide="right"`. One handle
 * with four parameters beats two handles that drift on keyboard behaviour.
 */
export function SidebarResizer({
  width,
  onResize,
  panelSide = 'left',
  min = SIDEBAR_MIN,
  max = SIDEBAR_MAX,
  label = 'Resize sidebar',
}: SidebarResizerProps) {
  const draggingRef = useRef(false)
  /** +1 when dragging right grows the panel, -1 when it shrinks it. */
  const dir = panelSide === 'left' ? 1 : -1

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault()
    draggingRef.current = true
    const startX = e.clientX
    const startW = width
    function onMove(ev: PointerEvent) {
      if (!draggingRef.current) return
      onResize(startW + dir * (ev.clientX - startX))
    }
    function onUp() {
      draggingRef.current = false
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }, [width, onResize, dir])

  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    // ← / → move the HANDLE; which way that grows the panel depends on the side
    // it is on, so the arrows mirror with the pointer rather than against it.
    if (e.key === 'ArrowLeft') { e.preventDefault(); onResize(width - dir * STEP) }
    else if (e.key === 'ArrowRight') { e.preventDefault(); onResize(width + dir * STEP) }
    else if (e.key === 'Home') { e.preventDefault(); onResize(min) }
    else if (e.key === 'End') { e.preventDefault(); onResize(max) }
  }, [width, onResize, dir, min, max])

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={width}
      aria-valuemin={min}
      aria-valuemax={max}
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
