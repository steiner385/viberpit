// The ⋯ overflow menu on a card row: the trigger, the popup, and the three
// behaviours that make it behave like a menu rather than a div that appears.
//
//  · Outside-click closes it (mirrors SessionActions).
//  · Escape closes it, handled on the CONTAINER so it works whether focus is on
//    the trigger or inside the list, and it stopPropagation()s — dismissing a
//    menu must not also detach from the session.
//  · An open menu is an OVERLAY: it registers with shell/useOverlayState so the
//    shell's shortcuts (↑/↓ over the list, ←/Esc out of a session) do not fire
//    through it, the same owner Dialog, Sheet and ui/Menu register with.
//
// The trigger takes focus when the menu opens. On macOS Safari and Firefox a
// click does not move focus to a <button>, and keydown goes to
// document.activeElement — so without this the Escape handler never fires for a
// mouse-opened menu while the overlay push keeps every global shortcut
// suppressed, and the keyboard goes silently inert. The TRIGGER, not the first
// item: it is what the markup names as the menu's control (`aria-haspopup` /
// `aria-expanded`), the items carry no roving tabindex to hand focus back to,
// and it stays mounted when the menu closes — so nothing has to restore focus,
// and Tab walks straight into the menu, which is the next thing in the DOM.
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { MoreVertical } from 'lucide-react'
import { cn } from '@/lib/utils'
import { popOverlay, pushOverlay } from '@/shell/useOverlayState'

/** Shared sizing for the per-row icon buttons: 44px tap target on touch, a
 *  denser 32px on hover-capable (desktop) pointers. */
// The two class constants below ship WITH the menu on purpose: `actionBtn` is
// the trigger's own sizing, and a card that renders a CardMenu is exactly the
// card that needs both. Splitting them into a styles module to satisfy Fast
// Refresh would put one concept in two files — the anti-pattern this extraction
// exists to remove — so the rule is waived here instead (same call as
// ui/Button.tsx's buttonVariants).
// eslint-disable-next-line react-refresh/only-export-components
export const actionBtn = cn(
  'h-11 w-11 [@media(hover:hover)]:h-8 [@media(hover:hover)]:w-8',
  'flex items-center justify-center rounded-md flex-shrink-0',
  'focus-visible:outline-none focus-visible:ring-2',
  // Press feedback: a quick scale-down + background darken so a tap/click is
  // unmistakable (hover alone gives no change once the cursor is already over
  // the button, and touch has no hover at all). active: fires on both.
  'transition-[background-color,transform] duration-100',
  'active:scale-90 active:duration-75',
)

/** Styling shared by every entry of a card's ⋯ menu. */
// eslint-disable-next-line react-refresh/only-export-components
export const menuItem = cn(
  'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
  'hover:bg-gray-100 dark:hover:bg-gray-700',
  'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-700',
)

/** Passive `·` divider between line-2 segments. */
export function Sep() {
  return <span aria-hidden="true" className="flex-shrink-0 text-gray-300 dark:text-gray-600">·</span>
}

export interface CardMenuProps {
  /** Accessible name of the trigger, e.g. "More actions for session Alpha". */
  label: string
  /** Items, given the `close` every one of them must call before acting. */
  children: (close: () => void) => ReactNode
}

export function CardMenu({ label, children }: CardMenuProps) {
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const close = () => setOpen(false)

  // Close on outside-click (mirrors SessionActions). Escape is the `onKeyDown`
  // on the container below — a component handling its own key, not a second
  // app-wide listener.
  useEffect(() => {
    if (!open) return
    function onDocClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])

  useEffect(() => {
    if (!open) return
    pushOverlay()
    return () => popOverlay()
  }, [open])

  useEffect(() => {
    if (open) triggerRef.current?.focus()
  }, [open])

  return (
    <div
      className="relative flex-shrink-0"
      ref={menuRef}
      onKeyDown={(e) => {
        if (open && e.key === 'Escape') {
          e.stopPropagation()
          setOpen(false)
        }
      }}
    >
      <button
        type="button"
        ref={triggerRef}
        className={cn(actionBtn, 'text-gray-500 dark:text-gray-400',
          'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:ring-blue-500',
          'active:bg-gray-200 dark:active:bg-gray-600')}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        title="More actions"
        onClick={() => setOpen((o) => !o)}
      >
        <MoreVertical size={16} aria-hidden="true" />
      </button>
      {open && (
        <div
          role="menu"
          className={cn(
            // z-20 keeps the open menu above the sticky group headers
            // (GroupHeader: `sticky top-0 z-10`); at an equal z-index a later
            // group's header would paint over a menu opened from a card above.
            'absolute top-full right-0 mt-1 min-w-[12rem] max-w-xs z-20',
            'rounded-md border border-gray-200 dark:border-gray-700',
            'bg-white dark:bg-gray-800 shadow-lg py-1',
          )}
        >
          {children(close)}
        </div>
      )}
    </div>
  )
}
