// The frame every row in the sessions list shares: the article, the status
// rail, the swipe edges and their foreground, the selection ring, the
// scroll-into-view, the list-mode focus, and the open control.
//
// It was inlined in SessionCard, which meant an archived row could either grow
// a second copy of all of it or go without — and it went without: no rail, no
// swipe, no keyboard row, a bare div. The split is by WHAT IS TRUE OF A ROW
// (here) versus WHAT A ROW SAYS (the card): `identity` goes inside the open
// button because it is passive, `trailing` sits outside it because chips and
// menus are interactive and must not nest inside a button.
//
// SessionCard's `pending` early-return deliberately stays in SessionCard: an
// archived row is never pending, and a flag only one caller can set is not a
// property of the frame.
import { useEffect, useRef, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { useSwipeAction } from '@/lib/useSwipeAction'

export interface CardSwipeAction {
  /** Accessible name of the revealed button, e.g. "Archive Alpha". */
  label: string
  icon: ReactNode
  onCommit: () => void
  /** Existing testids must be preserved: 'swipe-panel-archive' / 'swipe-panel-delete'. */
  testId: string
  /** Panel colour classes. */
  className: string
}

export interface CardFrameProps {
  id: string
  /** Accessible name of the open control, e.g. "Open session Alpha". */
  openLabel: string
  /** Left-edge status rail colour class. */
  railClass: string
  selected: boolean
  listMode: boolean
  onOpen: (id: string) => void
  /** Tab-stop for the open control. Omit for the roving-tabindex default
   *  (`selected ? 0 : -1`) SessionCard's keyboard cursor needs — a highlighted
   *  row takes the tab stop, an unhighlighted one yields it. A card with no
   *  roving cursor to manage (e.g. ArchivedSessionCard, every row a plain tab
   *  stop) should pass `true` so its open control is always reachable. */
  focusable?: boolean
  /** Row-level key handling (each card owns its own shortcut table). */
  onKeyDown?: (e: React.KeyboardEvent) => void
  /** Swipe right reveals this (reversible action). Omit to disable the edge. */
  swipeRight?: CardSwipeAction
  /** Swipe left reveals this (destructive). Omit to disable the edge. */
  swipeLeft?: CardSwipeAction
  /** Disable the gesture entirely (a pending row). */
  swipeEnabled?: boolean
  /** Rendered before the open button on line 1 (the sub-agent chevron). */
  leading?: ReactNode
  /** Passive content INSIDE the open button (dot, markers, title). */
  identity: ReactNode
  /** Interactive siblings after the open button (chips, actions, the ⋯ menu). */
  trailing?: ReactNode
  /** Line 2 and anything below it. */
  children?: ReactNode
}

/** Width of a revealed edge panel. */
const PANEL_PX = 88

export function CardFrame({
  id, openLabel, railClass, selected, listMode, onOpen, onKeyDown, focusable,
  swipeRight, swipeLeft, swipeEnabled = true, leading, identity, trailing, children,
}: CardFrameProps) {
  const articleRef = useRef<HTMLElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  // lg = the app's desktop breakpoint. jsdom reports false → mobile, so tests
  // exercise the gesture branch by default.
  const isWide = useMediaQuery('(min-width: 1024px)')
  const reduceMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const gestureLive = swipeEnabled && !isWide
  const swipe = useSwipeAction({
    enabled: gestureLive,
    onCommit: (side) => (side === 'left' ? swipeLeft?.onCommit() : swipeRight?.onCommit()),
  })
  // A settled openSide with no action configured for that edge (both current
  // callers wire both edges, but CardFrame itself does not require it) must
  // not reveal anything — no translate, no clip — or the user gets a bare
  // 88px gutter with nothing behind it, recoverable only by tapping the card.
  const revealValid = swipe.openSide === 'left' ? !!swipeLeft
    : swipe.openSide === 'right' ? !!swipeRight
      : false
  const restX = swipe.dragging
    ? swipe.dx
    : revealValid ? (swipe.openSide === 'left' ? -PANEL_PX : PANEL_PX) : 0
  const panelsMounted = gestureLive && (swipe.dragging || revealValid)

  // jsdom does not implement scrollIntoView.
  useEffect(() => {
    if (selected && articleRef.current?.scrollIntoView) {
      articleRef.current.scrollIntoView({ block: 'nearest' })
    }
  }, [selected])

  // In list-focus mode the highlighted row takes REAL focus, so single-key
  // shortcuts land here and never type into the prompt.
  useEffect(() => {
    if (listMode && selected) buttonRef.current?.focus()
  }, [listMode, selected])

  /** Whole-card click-to-open. A click on or inside an interactive descendant
   *  is that descendant's; everything else opens the row. */
  function handleCardClick(e: React.MouseEvent) {
    if ((e.target as HTMLElement).closest('button, a, [role="menu"]')) return
    if (swipe.openSide) { swipe.reset(); return }
    if (swipe.consumeDrag()) return
    onOpen(id)
  }

  function handleOpenKeyDown(e: React.KeyboardEvent) {
    if (!listMode) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        onOpen(id)
      }
      return
    }
    onKeyDown?.(e)
  }

  function renderPanel(side: 'left' | 'right', action: CardSwipeAction) {
    const revealed = swipe.openSide === side
    return (
      <button
        type="button"
        data-testid={action.testId}
        aria-label={action.label}
        // Hidden from assistive tech until revealed, so a screen reader does not
        // announce a phantom action behind every card.
        aria-hidden={!revealed}
        tabIndex={revealed ? 0 : -1}
        onClick={() => { swipe.reset(); action.onCommit() }}
        className={cn(
          'absolute inset-y-0 flex w-[88px] items-center justify-center',
          side === 'right' ? 'left-0' : 'right-0',
          action.className,
          revealed ? 'pointer-events-auto' : 'pointer-events-none',
        )}
      >
        {action.icon}
      </button>
    )
  }

  return (
    <article
      ref={articleRef}
      onClick={handleCardClick}
      className={cn(
        // `@container` makes the card its own container-query context so the
        // layout responds to the CARD's width (the drag-resizable sidebar), not
        // the viewport. Gated variants must stay ≤ @md — the content box tops
        // out near 503px, so @lg can never match inside the sidebar.
        'group @container relative flex flex-col rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 px-3 py-2 min-h-[44px] cursor-pointer',
        'focus-within:ring-2 focus-within:ring-blue-500',
        selected && 'ring-2 ring-blue-500 bg-blue-50 dark:bg-blue-950/30',
        // Clip the edge panels only while they are mounted. A permanent
        // overflow-hidden also clipped the ⋯ menu on wide touch devices.
        panelsMounted && 'overflow-hidden',
      )}
    >
      {/* Status rail — outside the swipe foreground, so triage colour holds
          while the card is being dragged. */}
      <span
        data-testid="card-status-rail"
        aria-hidden="true"
        className={cn('absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r-full', railClass)}
      />

      {panelsMounted && (
        <>
          {swipeRight && renderPanel('right', swipeRight)}
          {swipeLeft && renderPanel('left', swipeLeft)}
        </>
      )}

      <div
        {...(gestureLive ? { 'data-testid': 'card-swipe-fg', onPointerDown: swipe.onPointerDown } : {})}
        style={(gestureLive && restX !== 0) ? { transform: `translateX(${restX}px)` } : undefined}
        className={cn(
          gestureLive && 'relative bg-white dark:bg-gray-800 touch-pan-y',
          gestureLive && !swipe.dragging && !reduceMotion && 'transition-transform duration-200',
        )}
      >
        <div data-testid="card-identity" className="flex items-center gap-1.5 min-w-0">
          {leading}
          {/* `id` is the target of DispatchBar's aria-activedescendant: it
              signals the highlighted row without moving DOM focus.
              `aria-selected` is omitted on a plain button — axe rejects it
              without role="option". */}
          <button
            type="button"
            id={`row-${id}`}
            ref={buttonRef}
            tabIndex={focusable === undefined ? (selected ? 0 : -1) : (focusable ? 0 : -1)}
            data-selected={selected}
            className={cn(
              'flex-1 min-w-0 flex items-center gap-1.5 text-left py-0.5',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
            )}
            onClick={(e) => {
              // The article handler skips clicks on buttons — this IS the open
              // control, so it acts directly, with the same swipe guards.
              e.stopPropagation()
              if (swipe.openSide) { swipe.reset(); return }
              if (swipe.consumeDrag()) return
              onOpen(id)
            }}
            onKeyDown={handleOpenKeyDown}
            aria-label={openLabel}
          >
            {identity}
          </button>
          {trailing}
        </div>
        {children}
      </div>
    </article>
  )
}
